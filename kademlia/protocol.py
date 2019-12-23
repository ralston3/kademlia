import asyncio
import logging
import random
# pylint: disable=unused-wildcard-import,wildcard-import
from typing import *

from kademlia.node import Node, TNode
from kademlia.routing import RoutingTable
from kademlia.rpc import RPCProtocol
from kademlia.utils import digest, hex_to_base_int
from kademlia.storage import TForgetfulStorage

log = logging.getLogger(__name__)  # pylint: disable=invalid-name


RPCFindValueReturn = Union[List[Tuple[int, str, int]], Dict[str, Any]]

class KademliaProtocol(RPCProtocol):
	def __init__(self, source_node: TNode, storage: TForgetfulStorage, ksize: int):
		RPCProtocol.__init__(self)
		self.router = RoutingTable(self, ksize, source_node)
		self.storage = storage
		self.source_node = source_node

	def get_refresh_ids(self):
		"""
		Get list of node ids with which to search, in order to keep old
		buckets up to date.

		Parameters
		----------
			None

		Returns
		-------
			ids: List[int]
				ids of buckets that have not been updated since 3600
		"""
		ids = []
		for bucket in self.router.lonely_buckets():
			rid = random.randint(*bucket.range).to_bytes(20, byteorder='big')
			ids.append(rid)
		return ids

	def rpc_stun(self, sender: TNode) -> TNode:  # pylint: disable=no-self-use
		return sender

	def rpc_ping(self, sender: Tuple[str, int], node_id: int) -> int:
		"""
		Ping a given node

		Parameters
		----------
			sender: Tuple
				Address of sender that initiated ping
			node_id: int
				ID of sender that initated ping

		Returns
		-------
			int:
				ID of sending node
		"""
		source = Node(node_id, sender[0], sender[1])
		self.welcome_if_new(source)
		return self.source_node.id

	def rpc_store(self, sender: TNode, node_id: int, key: int, value: Any) -> bool:
		"""
		Store data from a given sender

		Parameters
		----------
			sender: Node
				Node that is initiating/requesting store
			node_id: int
				ID of node that is initiating/requesting store
			key: str
				ID of resource to be stored
			value: Any
				Payload to be stored at `key`

		Returns
		-------
			bool:
				Indicator of successful operation
		"""
		source = Node(node_id, sender[0], sender[1])
		self.welcome_if_new(source)
		log.debug("got a store request from %s, storing '%s'='%s'", sender, key.hex(), value)
		self.storage[key] = value
		return True

	def rpc_find_node(self, sender: TNode, node_id: int, key: int) -> List[Tuple[int, str, int]]:
		"""
		Find the node storing a given key

		Parameters
		----------
			sender: Node
				The node initiating the request
			node_id: int
				ID of the node initiating the request
			key: int
				ID of resource to be located

		Returns
		-------
			List[Tuple[int, str, int]]:
				Addresses of closest neighbors in regards to resource `key`
		"""
		log.info("finding neighbors of %i in local table", hex_to_base_int(node_id.hex()))
		source = Node(node_id, sender[0], sender[1])
		self.welcome_if_new(source)
		node = Node(key)
		neighbors = self.router.find_neighbors(node, exclude=source)
		return list(map(tuple, neighbors))

	# pylint: disable=line-too-long
	def rpc_find_value(self, sender: TNode, node_id: int, key: int) -> Union[List[Tuple[int, str, int]], Dict[str, Any]]:
		"""
		Return the value at a given key, via a given sender

		Parameters
		----------
			sender: Node
				Node at which key is stored
			node_id: int
				ID of node at which key is stored
			key: int
				ID of resource to be found

		Returns
		-------
			Union[List[Tuple[int, str, int]], Dict[str, Any]]:
				Will be either the given value indexed in a hashmap if the value is
				found, or will recursively attempt to find node at which key is
				stored via calls to `rpc_find_node`
		"""
		source = Node(node_id, sender[0], sender[1])
		self.welcome_if_new(source)
		value = self.storage.get(key, None)
		if value is None:
			return self.rpc_find_node(sender, node_id, key)
		return {"value": value}

	async def call_find_node(self, node_to_ask: TNode, node_to_find: TNode) -> List[Tuple[int, str, int]]:
		"""
		Dial a given node_to_ask in order to find node_to_find

		Parameters
		----------
			node_to_ask: Node
				Node to ask regarding node_to_find
			node_to_find: Node
				Node that this call is attempting to find

		Returns
		-------
			List[Tuple[int, str, int]]:
				Nodes closes to node_to_find which to continue search
		"""
		address = (node_to_ask.ip, node_to_ask.port)
		result = await self.find_node(address, self.source_node.id, node_to_find.id)
		return self.handle_call_response(result, node_to_ask)

	async def call_find_value(self, node_to_ask: TNode, node_to_find: TNode) -> Union[List[Tuple[int, str, int]], Dict[str, Any]]:
		"""
		Dial a given node_to_ask in order to find a value on node_to_find

		Parameters
		----------
			node_to_ask: Node
				Node to ask in order to find node_to_find to retrieve a given value
			node_to_find: Node
				Node that this call is attempting to find

		Returns
		-------
			Union[List[Tuple[int, str, int]], Dict[str, Any]]:
				Either the list of nodes clos'er' to the key associated with this
				value, or the actual value
		"""
		address = (node_to_ask.ip, node_to_ask.port)
		result = await self.find_value(address, self.source_node.id, node_to_find.id)
		return self.handle_call_response(result, node_to_ask)

	async def call_ping(self, node_to_ask):
		address = (node_to_ask.ip, node_to_ask.port)
		result = await self.ping(address, self.source_node.id)
		return self.handle_call_response(result, node_to_ask)

	async def call_store(self, node_to_ask, key, value):
		address = (node_to_ask.ip, node_to_ask.port)
		result = await self.store(address, self.source_node.id, key, value)
		return self.handle_call_response(result, node_to_ask)

	def welcome_if_new(self, node):
		"""
		Given a new node, send it all the keys/values it should be storing,
		then add it to the routing table.

		@param node: A new node that just joined (or that we just found out
		about).

		Process:
		For each key in storage, get k closest nodes.  If newnode is closer
		than the furtherst in that list, and the node for this server
		is closer than the closest in that list, then store the key/value
		on the new node (per section 2.5 of the paper)
		"""
		if not self.router.is_new_node(node):
			return

		log.info("never seen %s before, adding to router", node)
		for key, value in self.storage:
			keynode = Node(digest(key))
			neighbors = self.router.find_neighbors(keynode)
			if neighbors:
				last = neighbors[-1].distance_to(keynode)
				new_node_close = node.distance_to(keynode) < last
				first = neighbors[0].distance_to(keynode)
				this_closest = self.source_node.distance_to(keynode) < first
			if not neighbors or (new_node_close and this_closest):
				asyncio.ensure_future(self.call_store(node, key, value))
		self.router.add_contact(node)

	def handle_call_response(self, result: Any, node: TNode):
		"""
		If we get a response, add the node to the routing table.  If
		we get no response, make sure it's removed from the routing table.
		"""
		if not result[0]:
			log.warning("no response from %s, removing from router", node)
			self.router.remove_contact(node)
			return result

		log.info("got successful response from %s", node)
		self.welcome_if_new(node)
		return result

TKademliaProtocol = NewType("TKademliaProtocol", KademliaProtocol)
