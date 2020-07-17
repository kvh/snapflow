from __future__ import annotations

import json
import time
from collections import abc
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from itertools import tee
from typing import Any, Dict, Generator, List, Optional, Union, cast

import sqlalchemy
from sqlalchemy.engine import ResultProxy
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from basis.core.conversion import convert_lowest_cost
from basis.core.data_block import (
    DataBlock,
    DataBlockMetadata,
    DataSetMetadata,
    LocalMemoryDataRecords,
    ManagedDataBlock,
    StoredDataBlockMetadata,
    create_data_block_from_records,
)
from basis.core.data_formats import DataFrameGenerator, RecordsListGenerator
from basis.core.data_formats.base import ReusableGenerator
from basis.core.data_function import (
    DataFunctionDefinition,
    DataFunctionInterface,
    DataInterfaceType,
    InputExhaustedException,
)
from basis.core.data_function_interface import (
    BoundFunctionInterface,
    DataFunctionAnnotation,
    NodeInput,
    NodeInterfaceManager,
)
from basis.core.environment import Environment
from basis.core.metadata.orm import BaseModel
from basis.core.node import DataBlockLog, DataFunctionLog, Direction, Node
from basis.core.runtime import Runtime, RuntimeClass, RuntimeEngine
from basis.core.storage.storage import LocalMemoryStorageEngine, Storage
from basis.core.typing.object_type import ObjectType
from basis.utils.common import (
    BasisJSONEncoder,
    cf,
    error_symbol,
    get_spinner,
    printd,
    success_symbol,
    utcnow,
)
from loguru import logger


class Language(Enum):
    PYTHON = "python"
    SQL = "sql"


class LanguageDialect(Enum):
    CPYTHON = (Language.PYTHON, "cpython")
    POSTGRES = (Language.SQL, "postgres")
    MYSQL = (Language.SQL, "mysql")


@dataclass(frozen=True)
class LanguageDialectSupport:
    language_dialect: LanguageDialect
    version_support: str  # e.g. >3.6.3,<4.0


@dataclass(frozen=True)
class CompiledDataFunction:  # TODO: this is unused currently, just a dumb wrapper on the df
    name: str
    # code: str
    function: DataFunctionDefinition  # TODO: compile this to actual string code we can run aaannnnnnyyywhere
    # language_support: Iterable[LanguageDialect] = None  # TODO


@dataclass(frozen=True)
class RuntimeSpecification:
    runtime_class: RuntimeClass
    runtime_engine: RuntimeEngine
    runtime_version_requirement: str
    package_requirements: List[str]  # "extensions" for postgres, etc


@dataclass(frozen=True)
class Runnable:
    node_name: str
    compiled_data_function: CompiledDataFunction
    # runtime_specification: RuntimeSpecification # TODO: support this
    data_function_interface: BoundFunctionInterface
    configuration: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunSession:
    data_function_log: DataFunctionLog
    metadata_session: Session  # Make this a URL or other jsonable and then runtime can connect

    def log(self, block: DataBlockMetadata, direction: Direction):
        drl = DataBlockLog(  # type: ignore
            data_function_log=self.data_function_log,
            data_block=block,
            direction=direction,
            processed_at=utcnow(),
        )
        self.metadata_session.add(drl)

    def log_input(self, block: DataBlockMetadata):
        logger.debug(f"\t\tLogging Input {block}")
        self.log(block, Direction.INPUT)

    def log_output(self, block: DataBlockMetadata):
        logger.debug(f"\t\tLogging Output {block}")
        self.log(block, Direction.OUTPUT)


@dataclass  # (frozen=True)
class ExecutionContext:
    env: Environment
    metadata_session: Session
    storages: List[Storage]
    runtimes: List[Runtime]
    target_storage: Storage
    local_memory_storage: Storage
    current_runtime: Optional[Runtime] = None

    def clone(self, **kwargs):
        args = dict(
            env=self.env,
            metadata_session=self.metadata_session,
            storages=self.storages,
            runtimes=self.runtimes,
            target_storage=self.target_storage,
            local_memory_storage=self.local_memory_storage,
            current_runtime=self.current_runtime,
        )
        args.update(**kwargs)
        return ExecutionContext(**args)  # type: ignore

    @contextmanager
    def start_data_function_run(self, node: Node) -> Generator[RunSession, None, None]:
        assert self.current_runtime is not None, "Runtime not set"
        dfl = DataFunctionLog(  # type: ignore
            node_name=node.name,
            data_function_uri=node.data_function.uri,
            # data_function_config=node.data_function.configuration,  # TODO
            runtime_url=self.current_runtime.url,
            started_at=utcnow(),
        )
        try:
            yield RunSession(dfl, self.metadata_session)
        except Exception as e:
            dfl.set_error(e)
            raise e
        finally:
            dfl.completed_at = utcnow()

    def add(self, obj: BaseModel) -> BaseModel:
        try:
            self.metadata_session.add(obj)
        except InvalidRequestError as e:
            # Already in session perhaps
            logger.debug(f"Can't add obj {obj}: {e}")
        return self.merge(obj)

    def merge(self, obj: BaseModel) -> BaseModel:
        return self.metadata_session.merge(obj)

    @property
    def all_storages(self) -> List[Storage]:
        # TODO: should it be in the list of storages already?
        return [self.local_memory_storage] + self.storages

    # def to_json(self) -> str:
    #     return json.dumps(
    #         dict(
    #             env=self.env,
    #             storages=self.storages,
    #             runtimes=self.runtimes,
    #             target_storage=self.target_storage,
    #         ),
    #         cls=BasisJSONEncoder,
    #     )
    #
    # @classmethod
    # def from_json(cls, dct: Dict) -> ExecutionContext:
    #     return ExecutionContext(
    #         env=Environment.from_json(dct["env"]),
    #         storages=dct["storages"],
    #         runtimes=dct["runtimes"],
    #         target_storage=dct["target_storage"],
    #     )


@dataclass(frozen=True)
class DataFunctionContext:
    execution_context: ExecutionContext
    worker: Worker
    runnable: Runnable
    inputs: List[NodeInput]
    # resolved_output_otype: Optional[ObjectType]
    # realized_output_otype: Optional[ObjectType]

    def config(self, key: str) -> Any:
        return self.runnable.configuration.get(key)


class ExecutionManager:
    def __init__(self, ctx: ExecutionContext):
        self.ctx = ctx
        self.env = ctx.env

    def select_runtime(self, node: Node) -> Runtime:
        compatible_runtimes = node.data_function.compatible_runtime_classes
        for runtime in self.ctx.runtimes:
            if (
                runtime.runtime_class in compatible_runtimes
            ):  # TODO: Just taking the first one...
                return runtime
        raise Exception(
            f"No compatible runtime available for {node} (runtime class {compatible_runtimes} required)"
        )

    def get_bound_data_function_interface(self, node: Node) -> BoundFunctionInterface:
        dfi_mgr = NodeInterfaceManager(self.ctx, node)
        return dfi_mgr.get_bound_interface()

    def run(self, node: Node, to_exhaustion: bool = False) -> Optional[DataBlock]:
        if node.is_composite():
            raise NotImplementedError
        runtime = self.select_runtime(node)
        run_ctx = self.ctx.clone(current_runtime=runtime)
        worker = Worker(run_ctx)
        last_output: Optional[DataBlockMetadata] = None

        # Setup for run
        base_msg = (
            f"Running node: {cf.green(node.name)} {cf.dimmed(node.data_function.name)}"
        )
        spinner = get_spinner()
        # spinner.start(base_msg)
        start = time.time()
        n_outputs = 0
        n_runs = 0
        try:
            while True:
                dfi = self.get_bound_data_function_interface(node)
                df = node.data_function.get_definition(runtime.runtime_class)
                if df is None:
                    raise NotImplementedError(
                        f"No function definition found for {node.data_function.name} and runtime {runtime.runtime_class}"
                    )
                runnable = Runnable(
                    node_name=node.name,
                    compiled_data_function=CompiledDataFunction(
                        name=node.name, function=df,
                    ),
                    data_function_interface=dfi,
                    configuration=node.config,
                )
                last_output = worker.run(runnable)
                n_runs += 1
                if last_output is not None:
                    n_outputs += 1
                if (
                    not to_exhaustion or not dfi.inputs
                ):  # TODO: We just run no-input DFs (source extractors) once no matter what
                    break
            #     spinner.text = f"{base_msg}: {cf.blue}{cf.bold(n_outputs)} {cf.dimmed_blue}DataBlocks output{cf.reset} {cf.dimmed}{(time.time() - start):.1f}s{cf.reset}"
            # spinner.stop_and_persist(symbol=cf.success(success_symbol))
        except InputExhaustedException as e:  # TODO: i don't think we need this out here anymore (now that extractors don't throw)
            logger.debug(cf.warning("    Input Exhausted"))
            if e.args:
                logger.debug(e)
            if n_runs == 0:
                spinner.stop_and_persist(
                    symbol=cf.success(success_symbol),
                    text=f"{base_msg}: {cf.dimmed_bold}No unprocessed inputs{cf.reset}",
                )
            else:
                # spinner.stop_and_persist(symbol=cf.success(success_symbol))
                pass
        except Exception as e:
            # spinner.stop_and_persist(
            #     symbol=cf.error(error_symbol),
            #     text=f"{base_msg}: {cf.error('Error')} {cf.dimmed_red(e)}",
            # )
            raise e

        if last_output is None:
            return None
        new_session = self.env.get_new_metadata_session()
        last_output = new_session.merge(last_output)
        return last_output.as_managed_data_block(self.ctx,)

    #
    # def produce(
    #         self, node_like: Union[ConfiguredDataFunction, str]
    # ) -> Optional[DataBlock]:
    #     if isinstance(node_like, str):
    #         node_like = self.env.get_node(node_like)
    #     assert isinstance(node_like, ConfiguredDataFunction)
    #     dependencies = get_all_upstream_dependencies_in_execution_order(self.env, node_like)
    #     for dep in dependencies:
    #         with self.env.execution() as em:
    #             em.run(dep, to_exhaustion=True)


class Worker:
    def __init__(self, ctx: ExecutionContext):
        self.env = ctx.env
        self.ctx = ctx

    def run(self, runnable: Runnable) -> Optional[DataBlockMetadata]:
        output_block: Optional[DataBlockMetadata] = None
        node = self.env.get_node(runnable.node_name)
        with self.ctx.start_data_function_run(node) as run_session:
            output = self.execute_data_function(runnable)
            print("output", output)
            if output is not None:
                # assert (
                #     runnable.data_function_interface.resolved_output_otype is not None
                # )
                assert (
                    self.ctx.target_storage is not None
                ), "Must specify target storage for output"
                output_block = self.conform_output(run_session, output, runnable)
                # assert output_block is not None, output    # Output block may be none if empty generator
                if output_block is not None:
                    run_session.log_output(output_block)

            for input in runnable.data_function_interface.inputs:
                # assert input.bound_data_block is not None, input
                if input.bound_data_block is not None:
                    run_session.log_input(input.bound_data_block)
        return output_block

    def execute_data_function(self, runnable: Runnable) -> DataInterfaceType:
        args = []
        if runnable.data_function_interface.requires_data_function_context:
            dfc = DataFunctionContext(
                self.ctx,
                worker=self,
                runnable=runnable,
                inputs=runnable.data_function_interface.inputs,
                # resolved_output_otype=runnable.data_function_interface.resolved_output_otype,
                # realized_output_otype=runnable.data_function_interface.realized_output_otype,
            )
            args.append(dfc)
        inputs = runnable.data_function_interface.as_kwargs()
        mgd_inputs = {
            n: self.get_managed_data_block(block) for n, block in inputs.items()
        }
        assert (
            runnable.compiled_data_function.function.function_callable is not None
        ), f"Attempting to execute composite function directly {runnable.compiled_data_function.function}"
        return runnable.compiled_data_function.function.function_callable(
            *args, **mgd_inputs
        )

    def get_managed_data_block(self, block: DataBlockMetadata) -> ManagedDataBlock:
        return block.as_managed_data_block(self.ctx)

    def conform_output(
        self, worker_session: RunSession, output: DataInterfaceType, runnable: Runnable,
    ) -> Optional[DataBlockMetadata]:
        assert runnable.data_function_interface.output is not None
        # assert runnable.data_function_interface.resolved_output_otype is not None
        assert self.ctx.target_storage is not None
        # TODO: check if these Metadata objects have been added to session!
        #   also figure out what merge actually does
        if isinstance(output, StoredDataBlockMetadata):
            output = self.ctx.merge(output)
            return output.data_block
        if isinstance(output, DataBlockMetadata):
            output = self.ctx.merge(output)
            return output
        if isinstance(output, DataSetMetadata):
            output = self.ctx.merge(output)
            return output.data_block

        if isinstance(output, abc.Generator):
            if (
                runnable.data_function_interface.output.data_format_class
                == "DataFrameGenerator"
            ):
                output = DataFrameGenerator(output)
            elif (
                runnable.data_function_interface.output.data_format_class
                == "RecordsListGenerator"
            ):
                output = RecordsListGenerator(output)
            else:
                TypeError(output)
            output = cast(ReusableGenerator, output)
            if output.get_one() is None:
                # Empty generator
                return None
        block, sdb = create_data_block_from_records(
            self.env,
            self.ctx.metadata_session,
            self.ctx.local_memory_storage,
            output,
            # expected_otype=runnable.data_function_interface.resolved_output_otype,
        )
        # ldr = LocalMemoryDataRecords.from_records_object(output)
        # block = DataBlockMetadata(
        #     otype_uri=runnable.data_function_interface.output_otype.uri
        # )
        # sdb = StoredDataBlockMetadata(  # type: ignore
        #     data_block=block,
        #     storage_url=self.ctx.local_memory_storage.url,
        #     data_format=ldr.data_format,
        # )
        # block = self.ctx.add(block)
        # sdb = self.ctx.add(sdb)
        # LocalMemoryStorageEngine(
        #     self.env, self.ctx.local_memory_storage
        # ).store_local_memory_data_records(sdb, ldr)

        # TODO: check if existing storage_format is compatible with target storage, instead of using natural (no need to convert then)
        # Place output in target storage
        convert_lowest_cost(
            self.ctx,
            sdb,
            self.ctx.target_storage,
            self.ctx.target_storage.natural_storage_format,
        )
        return block

    # TODO: where does this sql stuff really belong?
    def get_connection(self) -> sqlalchemy.engine.Engine:
        if self.ctx.current_runtime is None:
            raise Exception("Current runtime not set")
        if self.ctx.current_runtime.runtime_class != RuntimeClass.DATABASE:
            raise Exception(f"Runtime not supported {self.ctx.current_runtime}")
        return sqlalchemy.create_engine(self.ctx.current_runtime.url)

    def execute_sql(self, sql: str) -> ResultProxy:
        logger.debug("Executing SQL:")
        logger.debug(sql)
        return self.get_connection().execute(sql)
