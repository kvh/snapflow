from __future__ import annotations

import enum
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Union

from sqlalchemy.orm import Session, relationship
from sqlalchemy.orm.relationships import RelationshipProperty
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import Column, ForeignKey
from sqlalchemy.sql.sqltypes import JSON, DateTime, Enum, Integer, String

from dags.core.data_block import DataBlock, DataBlockMetadata
from dags.core.environment import Environment
from dags.core.metadata.orm import BaseModel
from dags.core.pipe import Pipe, PipeLike, ensure_pipe, make_pipe, make_pipe_key
from dags.core.pipe_interface import PipeInterface
from loguru import logger

if TYPE_CHECKING:
    from dags.core.runnable import ExecutionContext
    from dags.core.streams import DataBlockStream
    from dags.core.graph import Graph


NodeLike = Union[str, "Node"]


def inputs_as_nodes(graph: Graph, inputs: Mapping[str, NodeLike]):
    return {name: graph.get_any_node(dnl) for name, dnl in inputs.items()}


def create_node(
    graph: Graph,
    key: str,
    pipe: Union[PipeLike, str],
    inputs: Optional[Union[NodeLike, Mapping[str, NodeLike]]] = None,
    dataset_name: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    declared_composite_node_key: str = None,
):
    config = config or {}
    env = graph.env
    if isinstance(pipe, str):
        pipe = env.get_pipe(pipe)
    else:
        pipe = make_pipe(pipe)
    interface = pipe.get_interface(env)
    _declared_inputs = {}
    _sub_nodes = []
    n = Node(
        env=graph.env,
        graph=graph,
        key=key,
        pipe=pipe,
        config=config,
        _dataset_name=dataset_name,
        interface=interface,
        declared_composite_node_key=declared_composite_node_key,
        _declared_inputs=_declared_inputs,
        _sub_nodes=_sub_nodes,
    )
    if inputs is not None:
        for i, v in interface.assign_inputs(inputs).items():
            n._declared_inputs[i] = v
    if n.is_composite():
        for sub_n in list(build_composite_nodes(n)):
            n._sub_nodes.append(sub_n)
    return n


@dataclass(frozen=True)
class Node:
    env: Environment
    graph: Graph
    key: str
    pipe: Pipe
    config: Mapping[str, Any]
    interface: PipeInterface
    _declared_inputs: Mapping[str, NodeLike]
    # _compiled_inputs: Optional[Mapping[str, Node]] = None
    _dataset_name: Optional[str] = None
    declared_composite_node_key: Optional[str] = None
    _sub_nodes: Optional[List[Node]] = None

    def __repr__(self):
        return f"<{self.__class__.__name__}(key={self.key}, pipe={self.pipe.key})>"

    def __hash__(self):
        return hash(self.key)

    # def set_compiled_inputs(self, inputs: Mapping[str, NodeLike]):
    #     self._compiled_inputs = inputs_as_nodes(self.graph, inputs)
    #     if self.is_composite():
    #         self.get_input_node().set_compiled_inputs(inputs)

    def get_state(self, sess: Session) -> Optional[Mapping]:
        state = sess.query(NodeState).filter(NodeState.node_key == self.key).first()
        if state:
            return state.state
        return None

    # def clone(self, new_name: str, **kwargs) -> Node:
    #     args = dict(
    #         env=self.env,
    #         key=new_name,
    #         pipe=self.pipe,
    #         config=self.config,
    #         inputs=self.get_declared_inputs(),
    #     )
    #     args.update(kwargs)
    #     return Node(**args)

    def get_dataset_node_key(self) -> str:
        return f"{self.key}__dataset"

    def get_dataset_name(self) -> str:
        return self._dataset_name or self.key

    def create_dataset_node(self, node_key: str = None) -> Node:
        dfi = self.get_interface()
        if dfi.output is None:
            raise
        lang = self.pipe.source_code_language()
        if dfi.output.data_format_class == "DataSet":
            if lang == "sql":
                df = "core.as_dataset_sql"
            else:
                df = "core.as_dataset"
        else:
            if lang == "sql":
                df = "core.sql_accumulate_as_dataset"
            else:
                df = "core.dataframe_accumulate_as_dataset"
        dsn = create_node(
            self.graph,
            key=node_key or self.get_dataset_node_key(),
            pipe=self.env.get_pipe(df),
            config={"dataset_name": self.get_dataset_name()},
            inputs=self,
        )
        logger.debug(f"Adding DataSet node {dsn}")
        return dsn

    def get_interface(self) -> PipeInterface:
        return self.interface

    def get_compiled_input_nodes(self) -> Mapping[str, Node]:
        # Just a convenience function
        return self.graph.get_flattened_graph().get_compiled_inputs(self)
        # return self._compiled_inputs or self.get_declared_input_nodes()

    def get_declared_input_nodes(self) -> Mapping[str, Node]:
        return inputs_as_nodes(self.graph, self.get_declared_inputs())

    def get_declared_inputs(self) -> Mapping[str, NodeLike]:
        return self._declared_inputs or {}

    # def get_compiled_input_keys(self) -> Mapping[str, str]:
    #     return {
    #         n: i.key if isinstance(i, Node) else i
    #         for n, i in self.get_compiled_input_nodes().items()
    #     }

    def get_sub_nodes(self) -> Optional[Iterable[Node]]:
        return self._sub_nodes

    def get_output_node(self) -> Node:
        if self.is_composite():
            assert self._sub_nodes is not None
            return self._sub_nodes[-1].get_output_node()
        return self

    def get_input_node(self) -> Node:
        if self.is_composite():
            assert self._sub_nodes is not None
            return self._sub_nodes[0].get_input_node()
        return self

    def is_composite(self) -> bool:
        return self.pipe.is_composite

    def as_stream(self) -> DataBlockStream:
        from dags.core.streams import DataBlockStream

        return DataBlockStream(upstream=self)

    def get_latest_output(self, ctx: ExecutionContext) -> Optional[DataBlock]:
        block = (
            ctx.metadata_session.query(DataBlockMetadata)
            .join(DataBlockLog)
            .join(PipeLog)
            .filter(
                DataBlockLog.direction == Direction.OUTPUT,
                PipeLog.node_key == self.key,
            )
            .order_by(DataBlockLog.created_at.desc())
            .first()
        )
        if block is None:
            return None
        return block.as_managed_data_block(ctx)


def build_composite_nodes(n: Node) -> Iterable[Node]:
    if not n.pipe.is_composite:
        raise
    nodes = []
    # TODO: just supports list for now
    raw_inputs = list(n.get_declared_inputs().values())
    assert len(raw_inputs) == 1, "Composite pipes take one input"
    input_node = raw_inputs[0]
    created_nodes = {}
    for fn in n.pipe.sub_graph:
        fn = ensure_pipe(n.env, fn)
        child_fn_key = make_pipe_key(fn)
        child_node_key = f"{n.key}__{child_fn_key}"
        try:
            if child_node_key in created_nodes:
                node = created_nodes[child_node_key]
            else:
                node = n.graph.get_declared_node(child_node_key)
        except KeyError:
            node = create_node(
                graph=n.graph,
                key=child_node_key,
                pipe=fn,
                config=n.config,
                inputs=input_node,
                declared_composite_node_key=n.declared_composite_node_key
                or n.key,  # Handle nested composite pipes
            )
            created_nodes[node.key] = node
        nodes.append(node)
        input_node = node
    return nodes

    # @property
    # def key(self):
    #     return self.key  # TODO


class NodeState(BaseModel):
    node_key = Column(String, primary_key=True)
    state = Column(JSON, nullable=True)

    def __repr__(self):
        return self._repr(node_key=self.node_key, state=self.state,)


def get_state(sess: Session, node_key: str) -> Optional[Dict]:
    state = sess.query(NodeState).filter(NodeState.node_key == node_key).first()
    if state:
        return state.state
    return None


class PipeLog(BaseModel):
    id = Column(Integer, primary_key=True, autoincrement=True)
    node_key = Column(String, nullable=False)
    node_start_state = Column(JSON, nullable=True)
    node_end_state = Column(JSON, nullable=True)
    pipe_key = Column(String, nullable=False)
    pipe_config = Column(JSON, nullable=True)
    runtime_url = Column(String, nullable=False)
    queued_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(JSON, nullable=True)
    data_block_logs: RelationshipProperty = relationship(
        "DataBlockLog", backref="pipe_log"
    )

    def __repr__(self):
        return self._repr(
            id=self.id,
            node_key=self.node_key,
            pipe_key=self.pipe_key,
            runtime_url=self.runtime_url,
            started_at=self.started_at,
        )

    def output_data_blocks(self) -> Iterable[DataBlockMetadata]:
        return [db for db in self.data_block_logs if db.direction == Direction.OUTPUT]

    def input_data_blocks(self) -> Iterable[DataBlockMetadata]:
        return [db for db in self.data_block_logs if db.direction == Direction.INPUT]

    def set_error(self, e: Exception):
        tback = traceback.format_exc()
        # Traceback can be v large (like in max recursion), so we truncate to 5k chars
        self.error = {"error": str(e), "traceback": tback[:5000]}

    def persist_state(self, sess: Session) -> NodeState:
        state = (
            sess.query(NodeState).filter(NodeState.node_key == self.node_key).first()
        )
        if state is None:
            state = NodeState(node_key=self.node_key)
        state.state = self.node_end_state
        return sess.merge(state)


class Direction(enum.Enum):
    INPUT = "input"
    OUTPUT = "output"

    @property
    def symbol(self):
        if self.value == "input":
            return "←"
        return "➞"

    @property
    def display(self):
        s = "out"
        if self.value == "input":
            s = "in"
        return self.symbol + " " + s


class DataBlockLog(BaseModel):
    id = Column(Integer, primary_key=True, autoincrement=True)
    pipe_log_id = Column(Integer, ForeignKey(PipeLog.id), nullable=False)
    data_block_id = Column(
        Integer, ForeignKey("dags_data_block_metadata.id"), nullable=False
    )  # TODO table name ref ugly here. We can parameterize with orm constant at least, or tablename("DataBlock.id")
    direction = Column(Enum(Direction, native_enum=False), nullable=False)
    processed_at = Column(DateTime, default=func.now(), nullable=False)
    # Hints
    data_block: "DataBlockMetadata"
    pipe_log: PipeLog

    def __repr__(self):
        return self._repr(
            id=self.id,
            pipe_log=self.pipe_log,
            data_block=self.data_block,
            direction=self.direction,
            processed_at=self.processed_at,
        )