from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, cast

import networkx as nx

from basis.core.data_block import DataBlock, DataBlockMetadata, DataSetMetadata
from basis.core.environment import Environment
from basis.core.typing.object_type import (
    ObjectType,
    ObjectTypeLike,
    ObjectTypeUri,
    is_any,
    is_generic,
)
from basis.utils.common import printd
from loguru import logger

if TYPE_CHECKING:
    from basis.core.data_function import (
        InputExhaustedException,
        DataFunctionCallable,
    )
    from basis.core.node import Node, Node, RawNodeInputs, NodeLike
    from basis.core.storage.storage import Storage
    from basis.core.runnable import ExecutionContext
    from basis.core.streams import (
        InputBlocks,
        DataBlockStream,
        ensure_data_stream,
        FunctionNodeInput,
        InputStreams,
    )


# re_type_hint = re.compile(
#     r"(?P<iterable>(Iterator|Iterable|Sequence|List)\[)?(?P<origin>\w+)(\[(?P<arg>(\w+\.)?\w+)\])?\]?"
# )
re_type_hint = re.compile(
    r"(?P<optional>(Optional)\[)?(?P<origin>\w+)(\[(?P<arg>(\w+\.)?\w+)\])?\]?"
)

VALID_DATA_INTERFACE_TYPES = [
    "DataBlock",
    "DataSet",
    "DataFrame",
    "RecordsList",
    "RecordsListGenerator",
    "DataFrameGenerator",
    "DatabaseTableRef",
    # TODO: is this list just a list of formats? which ones are valid i/o to DFs?
    # TODO: also, are DataBlocks the only valid *input* type?
    # "DatabaseCursor",
]

SELF_REF_PARAM_NAME = "this"


@dataclass
class DataFunctionAnnotation:
    data_format_class: str
    otype_like: ObjectTypeLike
    name: Optional[str] = None
    # is_iterable: bool = False  # TODO: what is state of iterable support?
    is_variadic: bool = False  # TODO: what is state of variadic support?
    is_generic: bool = False
    is_optional: bool = False
    is_self_ref: bool = False
    original_annotation: Optional[str] = None
    input_node: Optional[Node] = None
    bound_data_block: Optional[DataBlockMetadata] = None

    @property
    def is_dataset(self) -> bool:
        return self.data_format_class == "DataSet"

    @classmethod
    def create(cls, **kwargs) -> DataFunctionAnnotation:
        if not kwargs["otype_like"]:
            kwargs["otype_like"] = "Any"
        name = kwargs.get("name")
        if name:
            kwargs["is_self_ref"] = name == SELF_REF_PARAM_NAME
        otype_name = kwargs.get("otype_like")
        if isinstance(otype_name, str):
            kwargs["is_generic"] = is_generic(otype_name)
        if kwargs["data_format_class"] not in VALID_DATA_INTERFACE_TYPES:
            raise TypeError(
                f"`{kwargs['data_format_class']}` is not a valid data input type"
            )
        return DataFunctionAnnotation(**kwargs)

    @classmethod
    def from_parameter(cls, parameter: inspect.Parameter) -> DataFunctionAnnotation:
        annotation = parameter.annotation
        is_optional = parameter.default != inspect.Parameter.empty
        is_variadic = parameter.kind == inspect.Parameter.VAR_POSITIONAL
        tda = cls.from_type_annotation(
            annotation,
            name=parameter.name,
            is_optional=is_optional,
            is_variadic=is_variadic,
        )
        return tda

    @classmethod
    def from_type_annotation(cls, annotation: str, **kwargs) -> DataFunctionAnnotation:
        """
        Annotation of form `DataBlock[T]` for example
        """
        m = re_type_hint.match(annotation)
        if m is None:
            raise Exception(f"Invalid DataFunction annotation '{annotation}'")
        is_optional = bool(m.groupdict()["optional"])
        data_format_class = m.groupdict()["origin"]
        otype_name = m.groupdict()["arg"]
        args = dict(
            data_format_class=data_format_class,
            otype_like=otype_name,
            is_optional=is_optional,
            original_annotation=annotation,
        )
        args.update(**kwargs)
        return DataFunctionAnnotation.create(**args)  # type: ignore

    def otype_uri(self, env: Environment) -> ObjectTypeUri:
        if self.is_generic:
            raise  # TODO: ?? is this really an error? What is the URI of a generic otype?
        return env.get_otype(self.otype_like).uri


@dataclass
class NodeInput:
    name: str
    original_annotation: DataFunctionAnnotation
    input_node: Optional[Node] = None
    bound_data_block: Optional[DataBlockMetadata] = None


@dataclass
class BoundFunctionInterface:
    inputs: List[NodeInput]
    output: Optional[DataFunctionAnnotation]
    requires_data_function_context: bool = True

    def get_input(self, name: str) -> NodeInput:
        for input in self.inputs:
            if input.name == name:
                return input
        raise KeyError(name)

    def connect(self, input_nodes: Dict[str, Node]):
        for name, input_node in input_nodes.items():
            i = self.get_input(name)
            i.input_node = input_node

    def bind(self, input_blocks: Dict[str, DataBlockMetadata]):
        for name, input_block in input_blocks.items():
            i = self.get_input(name)
            i.bound_data_block = input_block

    @classmethod
    def from_dfi(cls, dfi: DataFunctionInterface) -> BoundFunctionInterface:
        return BoundFunctionInterface(
            inputs=[NodeInput(name=a.name, original_annotation=a) for a in dfi.inputs],
            output=dfi.output,
            requires_data_function_context=dfi.requires_data_function_context,
        )

    def as_kwargs(self):
        return {
            i.name: i.bound_data_block
            for i in self.inputs
            if i.bound_data_block is not None
        }


#
#     def bind_and_specify_otypes(self, env: Environment, input_blocks: InputBlocks):
#         if self.is_bound:
#             raise Exception("Already bound")
#         realized_generics: Dict[str, ObjectType] = {}
#         for name, input_block in input_blocks.items():
#             i = self.get_input(name)
#             i.bound_data_block = input_block
#             i.realized_otype = env.get_otype(input_block.realized_otype_uri)
#             if i.original_annotation.is_generic:
#                 assert isinstance(i.original_annotation.otype_like, str)
#                 realized_generics[i.original_annotation.otype_like] = i.realized_otype
#         if (
#             self.output is not None
#             and is_any(self.resolved_output_otype)
#             and self.output.is_generic
#         ):
#             # Further specify resolved type now that we have something concrete for Any
#             # TODO: man this is too complex. how do we simplify different type levels
#             assert isinstance(self.output.otype_like, str)
#             self.resolved_output_otype = realized_generics[self.output.otype_like]
#         self.is_bound = True
#
#     def as_kwargs(self):
#         if not self.is_bound:
#             raise Exception("Interface not bound")
#         return {i.name: i.bound_data_block for i in self.inputs}

# @classmethod
# def from_data_function_inteface(cls, dfi: DataFunctionInterface, input_blocks: InputBlocks) -> BoundFunctionInterface:
#     inputs = []
#     for name, input in input_blocks.items():
#         i = dfi.get_input(name)
#
#
#     return BoundFunctionInterface(
#         inputs=inputs,
#         output=dfi.output,
#         requires_data_function_context=dfi.requires_data_function_context,
#     )


@dataclass
class DataFunctionInterface:
    inputs: List[DataFunctionAnnotation]
    output: Optional[DataFunctionAnnotation]
    requires_data_function_context: bool = True
    # is_bound: bool = False

    @classmethod
    def from_data_function_definition(
        cls, df: DataFunctionCallable
    ) -> DataFunctionInterface:
        requires_context = False
        signature = inspect.signature(df)
        output = None
        ret = signature.return_annotation
        if ret is not inspect.Signature.empty:
            if not isinstance(ret, str):
                raise Exception("Return type annotation not a string")
            output = DataFunctionAnnotation.from_type_annotation(ret)
        inputs = []
        for name, param in signature.parameters.items():
            a = param.annotation
            if a is not inspect.Signature.empty:
                if not isinstance(a, str):
                    raise Exception("Parameter type annotation not a string")
            try:
                a = DataFunctionAnnotation.from_parameter(param)
                inputs.append(a)
            except TypeError:
                # Not a DataBlock/Set
                if param.annotation == "DataFunctionContext":
                    requires_context = True
                else:
                    raise Exception(f"Invalid data function parameter {param}")
        dfi = DataFunctionInterface(
            inputs=inputs,
            output=output,
            requires_data_function_context=requires_context,
        )
        dfi.validate_inputs()  # TODO: let caller handle this?
        return dfi

    def get_input(self, name: str) -> DataFunctionAnnotation:
        for input in self.inputs:
            if input.name == name:
                return input
        raise KeyError(name)

    def get_non_recursive_inputs(self):
        return [i for i in self.inputs if not i.is_self_ref]

    def get_inputs_dict(self) -> Dict[str, DataFunctionAnnotation]:
        return {i.name: i for i in self.inputs if i.name}

    def validate_inputs(self):
        # TODO: review this validation. what do we want to check for?
        data_block_seen = False
        for annotation in self.inputs:
            if (
                annotation.data_format_class == "DataBlock"
                and not annotation.is_optional
            ):
                if data_block_seen:
                    raise Exception(
                        "Only one uncorrelated DataBlock input allowed to a DataFunction."
                        "Correlate the inputs or use a DataSet"
                    )
                data_block_seen = True

    def assign_inputs(self, inputs: RawNodeInputs) -> Dict[str, NodeLike]:
        if not isinstance(inputs, dict):
            assert (
                len(self.get_non_recursive_inputs()) == 1
            ), f"Wrong number of inputs. (Variadic inputs not supported yet) {inputs}"
            return {self.get_non_recursive_inputs()[0].name: inputs}
        assert (set(inputs.keys()) - {"this"}) == set(
            i.name for i in self.get_non_recursive_inputs()
        ), f"{inputs}  {self.get_non_recursive_inputs()}"
        return inputs


class NodeInterfaceManager:
    """
    Responsible for finding and preparing DataBlocks for input to a
    Node.
    """

    def __init__(
        self, ctx: ExecutionContext, node: Node,
    ):
        self.env = ctx.env
        self.ctx = ctx
        self.node = node
        self.dfi = self.node.get_interface()

    def get_bound_interface(
        self, input_data_blocks: Optional[InputBlocks] = None
    ) -> BoundFunctionInterface:
        i = BoundFunctionInterface.from_dfi(self.dfi)
        i.connect(self.dfi.assign_inputs(self.node.get_raw_inputs()))
        if input_data_blocks is None:
            input_data_blocks = self.get_input_data_blocks()
        i.bind(input_data_blocks)
        return i

    def get_connected_interface(self) -> BoundFunctionInterface:
        i = BoundFunctionInterface.from_dfi(self.dfi)
        inputs = self.node.get_inputs(self.env)
        for input in i.inputs:
            if input.original_annotation.is_self_ref:
                inputs["this"] = self.node
        i.connect(inputs)
        return i

    def is_input_required(self, annotation: DataFunctionAnnotation) -> bool:
        if annotation.is_optional:
            return False
        # TODO: more complex logic? hmmmm
        return True

    def get_input_data_blocks(self) -> InputBlocks:
        from basis.core.streams import ensure_data_stream
        from basis.core.data_function import InputExhaustedException

        input_data_blocks: InputBlocks = {}
        any_unprocessed = False
        for input in self.get_connected_interface().inputs:
            stream = input.input_node
            logger.debug(f"Getting {input.name} for {stream}")
            stream = ensure_data_stream(stream)
            block: Optional[DataBlockMetadata] = self.get_input_data_block(
                stream, input, self.ctx.all_storages
            )
            logger.debug("\tFound:", block)

            """
            Inputs are considered "Exhausted" if:
            - Single DB stream (and zero or more DSs): no unprocessed DRs
            - Multiple correlated DB streams: ANY stream has no unprocessed DRs
            - One or more DSs: if ALL DS streams have no unprocessed

            In other words, if ANY DB stream is empty, bail out. If ALL DS streams are empty, bail
            """
            if block is None:
                logger.debug(
                    f"Couldnt find eligible DataBlocks for input `{input.name}` from {stream}"
                )
                if not input.original_annotation.is_optional:
                    # print(actual_input_node, annotation, storages)
                    raise InputExhaustedException(
                        f"    Required input '{input.name}'={stream} to DataFunction '{self.node.name}' is empty"
                    )
            else:
                input_data_blocks[input.name] = block
            if input.original_annotation.data_format_class == "DataBlock":
                any_unprocessed = True
            elif input.original_annotation.data_format_class == "DataSet":
                if block is not None:
                    any_unprocessed = any_unprocessed or stream.is_unprocessed(
                        self.ctx, block, self.node
                    )
            else:
                raise NotImplementedError

        if input_data_blocks and not any_unprocessed:
            raise InputExhaustedException("All inputs exhausted")

        return input_data_blocks

    def get_input_data_block(
        self, stream: DataBlockStream, input: NodeInput, storages: List[Storage] = None,
    ) -> Optional[DataBlockMetadata]:
        # TODO: Is it necessary to filter otype? We're already filtered on the `upstream` stream
        # if not input.is_generic:
        #     stream = stream.filter_otype(input.otype_like)
        if storages:
            stream = stream.filter_storages(storages)
        # # TODO: where do we do this parent node filtering? Such hidden, so magic.
        # #   There's the *delcared* input DBS and then this actual one, maybe a bit surprising to
        # #   end user that they differ
        # if input.parent_nodes:
        #     stream = stream.filter_upstream(input.parent_nodes)
        block: Optional[DataBlockMetadata]
        if input.original_annotation.data_format_class in ("DataBlock",):
            stream = stream.filter_unprocessed(
                self.node, allow_cycle=input.original_annotation.is_self_ref
            )
            block = stream.get_next(self.ctx)
        elif input.original_annotation.data_format_class == "DataSet":
            stream = stream.filter_dataset()
            block = stream.get_most_recent(self.ctx)
            # TODO: someday probably pass in actual DataSet (not underlying DB) to function that asks
            #   for it (might want to use `name`, for instance). and then just proxy
            #   through to underlying DB
        else:
            raise NotImplementedError

        return block
