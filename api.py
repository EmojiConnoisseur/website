#!/usr/bin/env python3
# encoding: utf-8

import asyncio
from datetime import datetime
import io
import json

from aiohttp import web
import discord
from emote_collector.utils import errors as emote_collector_errors
from emote_collector import utils as emote_collector_utils
import jinja2

from bot import *

app = web.Application(client_max_size=16 * 1024**2)  # controls max size of PUT/POST request data
routes = web.RouteTableDef()
api_prefix = '/api/v0'

environment = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))

def db_route(func):
	async def wrapped(request):
		try:
			return await func(request)
		except emote_collector_errors.EmoteNotFoundError:
			raise HTTPNotFound('emote does not exist')
		except emote_collector_errors.NoMoreSlotsError:
			raise HTTPInternalServiceError('no more slots')

	return wrapped

def requires_auth(func):
	func = db_route(func)

	async def authed_route(request):
		token = request.headers.get('Authorization')
		if not token:
			raise HTTPUnauthorized('no token provided')
		user_id = await api_cog.validate_token(token.encode())
		if not user_id:
			print(token)
			raise HTTPUnauthorized('invalid or incorrect token provided')

		request.user_id = user_id

		try:
			return await func(request)
		except emote_collector_errors.EmoteExistsError:
			raise HTTPConflict('emote exists', name=request.match_info['name'])
		except emote_collector_errors.EmoteDescriptionTooLongError as exception:
			raise HTTPBadRequest('emote description too long', limit=exception.limit)
		except emote_collector_errors.PermissionDeniedError:
			raise HTTPForbidden('you do not have permission to modify this emote')
		except discord.HTTPException as exception:
			status = exception.response.status
			if status == 400:
				cls = HTTPBadRequest
			elif status == 401:
				cls = HTTPUnauthorized
			elif status == 403:
				cls = HTTPForbidden
			elif status == 404:
				cls = HTTPNotFound

			raise cls(
				'HTTP error from Discord: {exception.text}'.format(exception=exception),
				error=dict(
					status=exception.response.status,
					reason=exception.response.reason,
					text=exception.text))

	return authed_route

async def get_emote_with_usage(name):
	emote = await db_cog.get_emote(name)
	emote.usage = await db_cog.get_emote_usage(emote)
	return emote_response(emote)

@routes.get(api_prefix+'/emote/{name}')
@db_route
async def emote(request):
	return await get_emote_with_usage(request.match_info['name'])

@routes.get(api_prefix+'/login')
@requires_auth
async def login(request):
	return web.json_response(str(request.user_id))

@routes.patch(api_prefix+'/emote/{name}')
@requires_auth
async def edit_emote(request):
	json = await request.json()

	actions = []

	name = request.match_info['name']
	await db_cog.get_emote(name)  # ensure it exists

	user_id = request.user_id

	if 'name' in json:
		actions.append(db_cog.rename_emote(name, json['name'], user_id))

	if 'description' in json:
		actions.append(db_cog.set_emote_description(name, user_id, json['description']))

	if not actions:
		raise HTTPBadRequest('no edits were specified')

	result = {}

	for action in actions:
		result = await action

	return emote_response(result)

@routes.delete(api_prefix+'/emote/{name}')
@requires_auth
async def delete_emote(request):
	name = request.match_info['name']
	user_id = request.user_id

	return emote_response(await db_cog.remove_emote(name, user_id))

@routes.put(api_prefix+'/emote/{name}/{url}')
@requires_auth
async def create_emote(request):
	name, url = map(request.match_info.get, ('name', 'url'))
	author = request.user_id

	try:
		return emote_response(await emotes_cog.add_from_url(name, url, author))
	except emote_collector_errors.URLTimeoutError:
		raise HTTPBadRequest('retrieving the image timed out')
	except emote_collector_errors.ImageResizeTimeoutError:
		raise HTTPRequestEntityTooLarge('resizing the image took too long')
	except ValueError:
		raise HTTPBadRequest('invalid URL')

@routes.put(api_prefix+'/emote/{name}')
@requires_auth
async def create_emote_from_data(request):
	if not request.has_body or not request.can_read_body:
		raise HTTPBadRequest('image data required in body')

	name = request.match_info['name']
	author = request.user_id
	image = io.BytesIO(await request.read())

	try:
		return emote_response(await emotes_cog.create_emote_from_bytes(name, author, image))
	except emote_collector_errors.ImageResizeTimeoutError:
		raise HTTPRequestEntityTooLarge('image resize took too long')
	except emote_collector_errors.InvalidImageError:
		raise HTTPUnsupportedMediaType('PNG, GIF, or JPEG required in body')

@routes.get(api_prefix+'/emotes')
async def list(request):
	results = await async_list(_marshaled_iterator(db_cog.all_emotes()))
	return json_or_not_found(results)

@routes.get(api_prefix+'/search/{query}')
async def search(request):
	results = await async_list(_marshaled_iterator(db_cog.search(request.match_info['query'])))
	return json_or_not_found(results)

@routes.get(api_prefix+'/popular')
async def popular(request):
	results = []
	async for emote in db_cog.popular_emotes():
		if emote.usage:
			results.append(_marshal_emote(emote))

	return json_or_not_found(results)

@routes.get(api_prefix+'/docs')
async def docs(request):
	return render_template('api_doc.html',
		urls=filter(None, (config['url'], *config['onions'].values())),
		prefix=config['prefix'])

app.add_routes(routes)

async def handle_404(request):
	raise HTTPNotFound

async def handle_500(request):
	raise HTTPInternalServerError

@web.middleware
async def error_middleware(request, handler):
	try:
		response = await handler(request)

		try:
			return await overrides[response.status](request)
		except KeyError:
			return response
	except web.HTTPException as exception:
		try:
			return await overrides[exception.status](request)
		except KeyError:
			raise exception

overrides = {
	404: handle_404,
	500: handle_500,
}

app.middlewares.append(error_middleware)

def _marshal_emote(emote):
	EPOCH = 1518652800  # February 15, 2018, the date of the first emote
	MAX_JSON_INT = 2**53

	allowed_fields = (
		'name',
		'id',
		'author',
		'animated',
		'created',
		'modified',
		'preserve',
		'description',
		'usage',
	)

	marshalled = {}

	for key in allowed_fields:
		try:
			value = emote[key]
		except KeyError:
			continue

		if isinstance(value, int) and value > MAX_JSON_INT:
			marshalled[key] = str(value)
		elif isinstance(value, datetime):
			marshalled[key] = int(value.timestamp()) - EPOCH
		else:
			marshalled[key] = value

	return marshalled

async def async_list(iterable):
	results = []
	async for x in iterable:
		results.append(x)
	return results

async def _marshaled_iterator(iterator):
	async for emote in iterator:
		yield _marshal_emote(emote)

def emote_response(emote):
	return web.json_response(_marshal_emote(emote))

def json_or_not_found(obj):
	if not obj:
		raise HTTPNotFound
	return web.json_response(obj)

def render_template(template, **kwargs):
	return web.Response(
		text=environment.get_template(template).render(**kwargs),
		content_type='text/html')

class JSONHTTPError(web.HTTPException):
	def __init__(self, reason=None, **kwargs):
		if reason:
			kwargs['message'] = reason

		super().__init__(
			text=json.dumps(dict(status=self.status_code, **kwargs)),
			content_type='application/json')

class HTTPBadRequest(JSONHTTPError, web.HTTPBadRequest):
	# god i love multiple inheritance
	pass

class HTTPUnauthorized(JSONHTTPError, web.HTTPUnauthorized):
	pass

class HTTPForbidden(JSONHTTPError, web.HTTPForbidden):
	pass

class HTTPNotFound(JSONHTTPError, web.HTTPNotFound):
	pass

class HTTPConflict(JSONHTTPError, web.HTTPConflict):
	pass

class HTTPRequestEntityTooLarge(JSONHTTPError, web.HTTPUnsupportedMediaType):
	pass

class HTTPUnsupportedMediaType(JSONHTTPError, web.HTTPUnsupportedMediaType):
	pass

class HTTPInternalServerError(JSONHTTPError, web.HTTPInternalServerError):
	pass
