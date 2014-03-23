#!/usr/bin/env python
# saged.py - Backend for websockets server in sage, a mobile CBus controller.
# Copyright 2012-2014 Michael Farrell <micolous+git@gmail.com>
# 
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.


from cbus.twisted_errors import *
from cbus.daemon.cdbusd import DBUS_INTERFACE, DBUS_SERVICE, DBUS_PATH
from twisted.internet import reactor, defer
from twisted.python import log
from twisted.web.server import Site
from twisted.web.static import File
from autobahn.twisted.websocket import WebSocketServerFactory, WebSocketServerProtocol
from autobahn.twisted.resource import WebSocketResource, HTTPChannelHixie76Aware
from autobahn.websocket.protocol import createWsUrl
from zope.interface import implements
from txdbus import client
from twisted.cred.portal import IRealm, Portal
from cbus.twisted_passlib import ApachePasswordDB
from twisted.web.resource import IResource
from twisted.web.guard import HTTPAuthSessionWrapper, BasicCredentialFactory
from json import loads, dumps
from argparse import ArgumentParser
import sys
from os.path import dirname, join, abspath

api = Factory = None

DEFAULT_SAGE_ROOT = abspath(join(dirname(__file__), '..', 'sage_root'))

class SageRealm(object):
	implements(IRealm)
	
	def __init__(self, root):
		self.root = root
	
	def requestAvatar(self, avatarId, mind, *interfaces):
		if IResource in interfaces:
			return (IResource, self.root, lambda: None)
		raise NotImplementedError('Only IResource interface is supported')


class DBusRemoteWrapperMethod(object):
	"""
	Wrapper for methods for interface.callRemote
	"""
	def __init__(self, obj, methname):
		self._obj = obj
		self._methname = methname


	def __call__(self, *args, **kwargs):
		return self._obj.callRemote(self._methname, *args, **kwargs)


class DBusRemoteWrapper(object):
	"""
	Wrapper for interfaces that makes everything a callRemote.

	"""
	def __init__(self, obj):
		self._obj = obj
	
	def __getattr__(self, name):
		return DBusRemoteWrapperMethod(self._obj, name)


class SageProtocol(WebSocketServerProtocol):
	def onConnect(self, request):
		WebSocketServerProtocol.onConnect(self, request)
		self.factory.clients.append(self)
		self.api = self.factory.api
	
	def send_object(self, obj):
		#print dumps(obj)
		self.sendMessage(dumps(obj))
	
	def send_states(self, *groups):
		print 'states = %r' % (self.api.get_light_states(groups),)
		states = [float(x) for x in self.api.get_light_states(groups)]
		self.send_object(dict(cmd='light_states', args=[dict(zip(groups, states))]))
		
	
	def onMessage(self, msg, binary):
		msg = loads(msg)
		
		cmd = msg[u'cmd']
		args = msg[u'args']
		
		# now try and handle the message
		if cmd == 'lighting_group_on':
			# handle lighting group on
			print "lighting group on %r" % args[0]
			groups = [int(x) for x in args[0]]
			
			if all((self.factory.allowed_by_policy(x) for x in groups)):
				self.api.lighting_group_on(groups)
			else:
				# group address denied by policy
				# return current light states
				self.send_states(*groups)
				return
				
			args = [groups]
		elif cmd == 'lighting_group_off':
			# handle lighting group off
			print 'lighting group off %r' % args[0]
			groups = [int(x) for x in args[0]]
			
			if all((self.factory.allowed_by_policy(x) for x in groups)):
				self.api.lighting_group_off(groups)
			else:
				# group address denied by policy
				# return current light states
				self.send_states(*groups)
				return
			
			args = [groups]
		elif cmd == 'lighting_group_ramp':
			# handle lighting ramp
			print 'lighting group ramp group=%s, duration=%s, level=%s' % (args[0], args[1], args[2])
			group = int(args[0])
			duration = int(args[1])
			level = float(args[2])
			
			if self.factory.allowed_by_policy(group):
				self.api.lighting_group_ramp(group, duration, level)
			else:
				self.send_states(group)
				return
			args = [group, duration, level]
		elif cmd == 'lighting_group_terminate_ramp':
			print 'lighting group terminate ramp group=%s' % args[0]
			group = int(args[0])
			
			self.api.lighting_group_terminate_ramp(group)
			args = [group]
		elif cmd == 'get_light_states':
			args = [int(x) for x in args]
			self.send_states(*args)
			
			# don't want to broadcast the request onto the network again.
			return
		else:
			print 'unknown command: %r' % cmd
			return
		
		#print repr(msg)
		
		# now send the message to other nodes
		# make sure args is sanitised before this point...
		args = [None] + args
		self.factory.broadcast_object(dict(cmd=cmd, args=args))
		
		#for c in self.factory.clients:
		#	if c != self:
		#		c.send_object(dict(cmd=cmd, args=args))
		
		#self.sendMessage(dumps(msg))
	
	def connectionLost(self, reason):
		try:
			self.factory.clients.remove(self)
		except ValueError:
			# client doesn't exist, pass
			pass
		WebSocketServerProtocol.connectionLost(self, reason)
		

class SageProtocolFactory(WebSocketServerFactory):
	def __init__(self, *args, **kwargs):
		# pop api parameter off
		self.api = kwargs.pop('api', None)
		self.allow_ga = kwargs.pop('allow_ga', None)
		self.deny_ga = kwargs.pop('deny_ga', None)
		
		kwargs['server'] = 'saged/0.1.0 (libcbus)'
		
		WebSocketServerFactory.__init__(self, *args, **kwargs)

		self.clients = []

	
	def broadcast_object(self, msg, exceptClient=None):
		# format into json once
		msg = dumps(msg)
		
		# broadcast
		for client in self.clients:
			if exceptClient == None or exceptClient != client:
				client.sendMessage(msg)
		
	def on_lighting_group_on(self, source_addr, group_addr):
		self.broadcast_object(dict(cmd='lighting_group_on', args=[int(source_addr), [int(group_addr)]]))
	
	def on_lighting_group_off(self, source_addr, group_addr):
		self.broadcast_object(dict(cmd='lighting_group_off', args=[int(source_addr), [int(group_addr)]]))
	
	def on_lighting_group_ramp(self, source_addr, group_addr, duration, level):
		self.broadcast_object(dict(cmd='lighting_group_ramp', args=[int(source_addr), int(group_addr), int(duration), float(level)]))
		
	def allowed_by_policy(self, group_address):
		"""
		Check what the policy for the group address should be.
		
		Returns True to allow, False to deny.
		"""
		
		if self.allow_ga != None:
			return group_address in self.allow_ga
		elif self.deny_ga != None:
			return group_address not in self.deny_ga
		else:
			return True


@defer.inlineCallbacks
def boot(listen_addr='127.0.0.1', port=8080, session_bus=False, sage_www_root=DEFAULT_SAGE_ROOT, auth_realm=None, auth_passwd=None, allow_ga=None, deny_ga=None, no_www=False):

	assert not (allow_ga and deny_ga), 'Must not specify both deny and allow rules for group addresses'
	global api
	global factory
	
	conn = yield client.connect(reactor, 'session' if session_bus else 'system')
	obj = yield conn.getRemoteObject(DBUS_SERVICE, DBUS_PATH)
	api = DBusRemoteWrapper(obj)

	uri = createWsUrl(listen_addr, port)
	factory = SageProtocolFactory(uri, debug=False, api=api, allow_ga=allow_ga, deny_ga=deny_ga)
	factory.setProtocolOptions(allowHixie76=True, webStatus=False)
	factory.protocol = SageProtocol
	factory.clients = []

	# register signals
	for n, m in (
		('on_lighting_group_on', factory.on_lighting_group_on),
		('on_lighting_group_off', factory.on_lighting_group_off),
		('on_lighting_group_ramp', factory.on_lighting_group_ramp)
	):
		obj.notifyOnSignal(n, m)

	resource = WebSocketResource(factory)
	
	if no_www:
		root = resource
	else:
		root = File(sage_www_root)
		root.putChild('saged', resource)
	
	if auth_realm != None and auth_passwd != None:
		portal = Portal(SageRealm(root), [ApachePasswordDB(auth_passwd)])
		credentialFactories = [BasicCredentialFactory(auth_realm),]
		root = HTTPAuthSessionWrapper(portal, credentialFactories)
		
	
	site = Site(root)
	site.protocol = HTTPChannelHixie76Aware
	reactor.listenTCP(port, site, interface=listen_addr)


if __name__ == '__main__':
	# do commandline handling
	parser = ArgumentParser(usage='%(prog)s')
	
	#parser.add_argument('-r', '--root-path',
	#	dest='root_path',
	#	default='cbus/sage_root',
	#	help='Root path of the sage webserver.  Used to serve the accompanying javascript and HTML content [default: %(default)s]'
	#)
	
	parser.add_argument('-H', '--listen-addr',
		dest='listen_addr',
		default='127.0.0.1',
		help='IP address to listen the web server on [default: %(default)s]'
	)
	
	parser.add_argument('-p', '--port',
		dest='port',
		type=int,
		default=8080,
		help='Port to run the web server on [default: %(default)s]'
	)
	
	parser.add_argument('-l', '--log',
		dest='log_target',
		type=file,
		default=sys.stdout,
		help='Log target [default: stdout]'
	)
	
	parser.add_argument('-S', '--session-bus',
		action='store_true',
		dest='session_bus',
		default=False,
		help='Bind to the session bus instead of the system bus [default: %(default)s]'
	)
	
	parser.add_argument('-r', '--sage-root',
		dest='sage_www_root',
		default=DEFAULT_SAGE_ROOT,
		help='Root path where sage www resources are stored [default: %(default)s]'
	)
	
	parser.add_argument('-W', '--no-www',
		dest='no_www',
		action='store_true',
		default=False,
		help='Disable serving any of the static web pages from the web server.  This is useful if you are hosting the sage webui files on a different web server, and only want saged to present a WebSockets interface [default: %(default)s]'
	)
	
	group = parser.add_argument_group('Authentication options')
	
	group.add_argument('-R', '--realm',
		dest='auth_realm',
		default='saged',
		help='HTTP authorisation realm to use for authenticating clients [default: %(default)s]'
	)
	
	group.add_argument('-P', '--passwd',
		dest='auth_passwd',
		required=False,
		help='If specified, a htpasswd password list to authenticate users with.  If not specified, no authentication will be used with this saged instance.  Note: due to a bug in Chrome (#123862), it cannot connect to password-protected WebSockets instances.'
	)
	
	group = parser.add_mutually_exclusive_group()
	
	group.add_argument('-a', '--allow-ga',
		dest='allow_ga',
		required=False,
		help='If specified, a comma seperated list of group addresses to allow "change" access to.  Other group addresses will be denied access.  saged will always report activities denied group addresses on the network.'
	)
	
	group.add_argument('-d', '--deny-ga',
		dest='deny_ga',
		required=False,
		help='If specified, a comma seperated list of group addresses to deny "change" access to.  Other group addresses will be allowed access.  saged will always report activities denied group addresses on the network.'
	)
	
	option = parser.parse_args()
	
	log.startLogging(option.log_target)
	
	if option.allow_ga != None:
		option.allow_ga = [int(x) for x in option.allow_ga.split(',')]
	if option.deny_ga != None:
		option.deny_ga = [int(x) for x in option.deny_ga.split(',')]
	
	reactor.callWhenRunning(boot, option.listen_addr, option.port, option.session_bus, option.sage_www_root, option.auth_realm, option.auth_passwd, option.allow_ga, option.deny_ga, option.no_www)
	
	reactor.run()	
