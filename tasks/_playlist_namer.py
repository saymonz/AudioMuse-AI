# AudioMuse-AI - https://github.com/NeptuneHub/AudioMuse-AI
# Copyright (C) 2025 NeptuneHub
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root or <https://github.com/NeptuneHub/AudioMuse-AI/blob/main/LICENSE>

"""Resolves the clustering playlist naming and cleanup backend.

The default is the built-in ``_automatic`` suffix scheme. A plugin can replace
it atomically by registering an implementation with
``ctx.register_clustering_provider('playlist_naming', ...)``.

Required surface on the replacement object/module:
* ``format_name(base_name, chunk_index=None) -> str``
* ``should_delete(playlist) -> bool``

Optional hooks on the replacement object/module:
* ``unformat_name(playlist_name) -> str | None``: strip plugin affixes before
  feeding historical names back into AI naming avoidance.
* ``on_created(playlist, playlist_name) -> None``: called after the media
  server creates one clustering playlist (for ID-tracking plugins).
* ``after_cleanup(deleted_ids, playlists) -> None``: called once after cleanup
  deletes matching playlists on the active server.

Main Features:
* Consults loaded plugins for a 'playlist_naming' clustering provider first
* Falls back to the built-in ``_automatic`` namer when no plugin registered one
  or when the replacement is missing part of ``REQUIRED_METHODS``
* Preserves the server context captured at resolution across every provider call
* Isolates per-call errors so a broken plugin never aborts clustering and never
  wipes unrelated user playlists
"""

from __future__ import annotations

import contextlib
import logging

from .mediaserver import context as ms_context

logger = logging.getLogger(__name__)

REQUIRED_METHODS = ('format_name', 'should_delete')


class _DefaultPlaylistNamer:
    owner = None

    def format_name(self, base_name, chunk_index=None):
        if chunk_index is not None:
            return f"{base_name}_automatic ({chunk_index})"
        return f"{base_name}_automatic"

    def should_delete(self, playlist):
        return ((playlist or {}).get('Name') or '').endswith('_automatic')

    def unformat_name(self, playlist_name):
        return playlist_name

    def on_created(self, playlist, playlist_name):
        return None

    def after_cleanup(self, deleted_ids, playlists):
        return None


_DEFAULT_NAMER = _DefaultPlaylistNamer()


class _PluginPlaylistNamer:
    def __init__(self, backend, owner, server=None):
        self._backend = backend
        self.owner = owner or 'unknown'
        self._server = server

    @contextlib.contextmanager
    def _bound(self):
        if self._server is not None and ms_context.active_server() is None:
            with ms_context.use_server(self._server):
                yield
        else:
            yield

    def format_name(self, base_name, chunk_index=None):
        fallback = _DEFAULT_NAMER.format_name(base_name, chunk_index)
        try:
            with self._bound():
                result = self._backend.format_name(base_name, chunk_index=chunk_index)
        except Exception:
            logger.exception(
                "Plugin %s: format_name failed for %r; using the built-in _automatic name instead",
                self.owner, base_name,
            )
            return fallback
        if not isinstance(result, str) or not result.strip():
            logger.warning(
                "Plugin %s: format_name(%r, %r) returned an empty or non-string value %r; "
                "using the built-in _automatic name instead",
                self.owner, base_name, chunk_index, result,
            )
            return fallback
        return result

    def should_delete(self, playlist):
        try:
            with self._bound():
                return bool(self._backend.should_delete(playlist))
        except Exception:
            playlist_id = (playlist or {}).get('Id') or (playlist or {}).get('id')
            logger.exception(
                "Plugin %s: should_delete failed for playlist %r; skipping deletion",
                self.owner, playlist_id,
            )
            return False

    def unformat_name(self, playlist_name):
        value = self._call_optional('unformat_name', playlist_name)
        return value if isinstance(value, str) and value.strip() else playlist_name

    def on_created(self, playlist, playlist_name):
        if playlist is not None:
            self._call_optional('on_created', playlist, playlist_name)

    def after_cleanup(self, deleted_ids, playlists):
        self._call_optional('after_cleanup', deleted_ids, playlists)

    def _call_optional(self, name, *args):
        fn = getattr(self._backend, name, None)
        if not callable(fn):
            return None
        try:
            with self._bound():
                return fn(*args)
        except Exception:
            logger.exception("Plugin %s: %s hook failed", self.owner, name)
            return None


def get_playlist_namer():
    override, owner = _resolve_plugin_backend()
    if override is not None and _has_required_surface(override, owner):
        return _PluginPlaylistNamer(override, owner, server=ms_context.active_server())
    return _DEFAULT_NAMER


def _resolve_plugin_backend():
    try:
        from plugin.manager import plugin_manager

        backend = plugin_manager.get_clustering_provider('playlist_naming')
        return backend, plugin_manager.clustering_provider_owner('playlist_naming')
    except Exception:
        return None, None


def _has_required_surface(backend, owner):
    missing = [
        name for name in REQUIRED_METHODS if not callable(getattr(backend, name, None))
    ]
    if not missing:
        return True
    logger.warning(
        "Plugin %s: its playlist_naming provider %r is missing %s; "
        "using the built-in _automatic naming instead",
        owner or 'unknown', backend, ', '.join(missing),
    )
    return False
