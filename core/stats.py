from typing import Dict, Any, Optional, Iterator
from datetime import datetime
from contextlib import contextmanager
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from HLL import HyperLogLog

from core.client import Client
from core.models import User
from util.json_type import JSONType
import settings

class Stats:
	__slots__ = ('logged_in', 'by_client', '_client_id_cache')
	
	logged_in: int
	by_client: Dict[int, Dict[str, Any]]
	_client_id_cache: Optional[Dict[Client, int]]
	
	def __init__(self) -> None:
		self.logged_in = 0
		self.by_client = {}
		self._client_id_cache = None
		
		hour = _current_hour()
		with Session() as sess:
			current = sess.query(CurrentStats).filter(CurrentStats.key == 'current_hour').one_or_none()
			if not current:
				return
			if current.value['hour'] != hour:
				return
			self.by_client = {
				int(client_id): _stats_from_json(stats)
				for client_id, stats in current.value['by_client'].items()
			}
	
	def on_login(self) -> None:
		self.logged_in += 1
	
	def on_logout(self) -> None:
		self.logged_in -= 1
	
	def on_user_active(self, user: User, client: Client) -> None:
		self._collect('users_active', user, client)
	
	def on_message_sent(self, user: User, client: Client) -> None:
		self._collect('messages_sent', user, client)
	
	def on_message_received(self, user: User, client: Client) -> None:
		self._collect('messages_received', user, client)
	
	def _collect(self, stat: str, user: User, client: Client) -> None:
		assert user is not None
		assert client is not None
		if self.by_client is None:
			self.by_client = {}
		bc = self.by_client
		client_id = self._get_client_id(client)
		if client_id not in bc:
			bc[client_id] = {}
		bhc = bc[client_id]
		if stat == 'users_active':
			if stat not in bhc:
				bhc[stat] = HyperLogLog(12)
			bhc[stat].add(user.email)
		else:
			if stat not in bhc:
				bhc[stat] = 0
			bhc[stat] += 1
	
	def flush(self) -> None:
		hour = _current_hour()
		now = datetime.utcnow()
		
		with Session() as sess:
			current = sess.query(CurrentStats).filter(CurrentStats.key == 'logged_in').one_or_none()
			if not current:
				current = CurrentStats(key = 'logged_in')
			current.date_updated = now
			current.value = self.logged_in
			sess.add(current)
			sess.flush()
			
			current = sess.query(CurrentStats).filter(CurrentStats.key == 'current_hour').one_or_none()
			if not current:
				current = CurrentStats(key = 'current_hour', value = { 'hour': hour })
			
			cs_hour = current.value['hour']
			current.date_updated = now
			current.value = self._flush_to_hourly(sess, hour)
			sess.add(current)
			
			if cs_hour != hour:
				self.by_client = {}
	
	def _flush_to_hourly(self, sess: Any, hour: int) -> Dict[str, Any]:
		for client_id, stats in self.by_client.items():
			hcs_opt = sess.query(HourlyClientStats).filter(HourlyClientStats.hour == hour, HourlyClientStats.client_id == client_id).one_or_none()
			if hcs_opt is None:
				hcs = HourlyClientStats(hour = hour, client_id = client_id)
			else:
				hcs = hcs_opt
			hcs.messages_sent = stats.get('messages_sent') or 0
			hcs.messages_received = stats.get('messages_received') or 0
			if 'users_active' in stats:
				hcs.users_active = stats['users_active'].cardinality()
			else:
				hcs.users_active = 0
			sess.add(hcs)
		return {
			'hour': hour,
			'by_client': {
				client_id: _stats_to_json(stats)
				for client_id, stats in self.by_client.items()
			}
		}
	
	def _get_client_id(self, client: Client) -> int:
		if self._client_id_cache is None:
			with Session() as sess:
				self._client_id_cache = {
					Client.FromJSON(row.data): row.id
					for row in sess.query(DBClient).all()
				}
		if client not in self._client_id_cache:
			with Session() as sess:
				dbobj = DBClient(data = Client.ToJSON(client))
				sess.add(dbobj)
				sess.flush()
				self._client_id_cache[client] = dbobj.id
		return self._client_id_cache[client]

def _stats_to_json(stats: Dict[str, Any]) -> Dict[str, Any]:
	json = {}
	if 'messages_sent' in stats:
		json['messages_sent'] = stats['messages_sent']
	if 'messages_received' in stats:
		json['messages_received'] = stats['messages_received']
	if 'users_active' in stats:
		json['users_active'] = list(stats['users_active'].registers())
	return json

def _stats_from_json(json: Dict[str, Any]) -> Dict[str, Any]:
	stats = {}
	if 'messages_sent' in json:
		stats['messages_sent'] = json['messages_sent']
	if 'messages_received' in json:
		stats['messages_received'] = json['messages_received']
	if 'users_active' in json:
		hll = HyperLogLog(12)
		hll.set_registers(bytearray(json['users_active']))
		stats['users_active'] = hll
	return stats

def _current_hour() -> int:
	now = datetime.utcnow()
	ts = now.timestamp()
	return int(ts // 3600)

class Base(declarative_base()): # type: ignore
	__abstract__ = True

class DBClient(Base):
	__tablename__ = 't_client'
	
	id = sa.Column(sa.Integer, nullable = False, primary_key = True)
	data = sa.Column(JSONType, nullable = False)

class HourlyClientStats(Base):
	__tablename__ = 't_stats_hour_client'
	
	hour = sa.Column(sa.Integer, nullable = False, primary_key = True)
	client_id = sa.Column(sa.Integer, nullable = False, primary_key = True)
	users_active = sa.Column(sa.Integer, nullable = False, server_default = '0')
	messages_sent = sa.Column(sa.Integer, nullable = False, server_default = '0')
	messages_received = sa.Column(sa.Integer, nullable = False, server_default = '0')

class CurrentStats(Base):
	__tablename__ = 't_stats_current'
	
	key = sa.Column(sa.String, nullable = False, primary_key = True)
	date_updated = sa.Column(sa.DateTime, nullable = False)
	value = sa.Column(JSONType, nullable = False)

engine = sa.create_engine(settings.STATS_DB)
session_factory = sessionmaker(bind = engine)

@contextmanager
def Session() -> Iterator[Any]:
	if Session._depth > 0: # type: ignore
		yield Session._global # type: ignore
		return
	session = session_factory()
	Session._global = session # type: ignore
	Session._depth += 1 # type: ignore
	try:
		yield session
		session.commit()
	except:
		session.rollback()
		raise
	finally:
		session.close()
		Session._global = None # type: ignore
		Session._depth -= 1 # type: ignore
Session._global = None # type: ignore
Session._depth = 0 # type: ignore
