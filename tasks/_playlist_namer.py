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
* Isolates per-call errors so a broken plugin never aborts clustering or wipes
  unrelated user playlists
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

REQUIRED_METHODS = ('format_name', 'should_delete')


def _default_format_name(base_name, chunk_index=None):
    if chunk_index is not None:
        return f"{base_name}_automatic ({chunk_index})"
    return f"{base_name}_automatic"


class _DefaultPlaylistNamer:
    is_default = True
    owner = None

    def format_name(self, base_name, chunk_index=None):
        return _default_format_name(base_name, chunk_index)

    def should_delete(self, playlist):
        return ((playlist or {}).get('Name') or '').endswith('_automatic')

    def unformat_name(self, playlist_name):
        return playlist_name

    def on_created(self, playlist, playlist_name):
        return None

    def after_cleanup(self, deleted_ids, playlists):
        return None

    def assign_chunks(self, final_songs, max_songs, final_name, base_name, final_playlists):
        if max_songs > 0 and len(final_songs) > max_songs:
            for offset in range(0, len(final_songs), max_songs):
                idx = offset // max_songs + 1
                final_playlists[f"{base_name} ({idx})"] = final_songs[offset : offset + max_songs]
        else:
            final_playlists[base_name] = final_songs


class _PluginPlaylistNamer:
    is_default = False

    def __init__(self, backend, owner):
        self._backend = backend
        self.owner = owner or 'unknown'

    def _invoke_format(self, base_name, chunk_index):
        if chunk_index is None:
            return self._backend.format_name(base_name)
        try:
            return self._backend.format_name(base_name, chunk_index=chunk_index)
        except TypeError:
            formatted = self._backend.format_name(base_name)
            return f"{formatted} ({chunk_index})" if isinstance(formatted, str) else formatted

    def format_name(self, base_name, chunk_index=None):
        try:
            result = self._invoke_format(base_name, chunk_index)
        except Exception:
            logger.exception(
                "Plugin %s: format_name failed for %r; using the built-in _automatic name instead",
                self.owner, base_name,
            )
            return _default_format_name(base_name, chunk_index)
        if not isinstance(result, str) or not result.strip():
            logger.warning(
                "Plugin %s: format_name(%r) returned an empty or non-string value %r; "
                "using the built-in _automatic name instead",
                self.owner, base_name, result,
            )
            return _default_format_name(base_name, chunk_index)
        return result

    def should_delete(self, playlist):
        try:
            return bool(self._backend.should_delete(playlist))
        except Exception:
            playlist_id = (playlist or {}).get('Id') or (playlist or {}).get('id')
            logger.exception(
                "Plugin %s: should_delete failed for playlist %r; skipping deletion",
                self.owner, playlist_id,
            )
            return False

    def unformat_name(self, playlist_name):
        fn = getattr(self._backend, 'unformat_name', None)
        if not callable(fn):
            return playlist_name
        try:
            value = fn(playlist_name)
        except Exception:
            logger.exception(
                "Plugin %s: unformat_name failed for %r; keeping stored name",
                self.owner, playlist_name,
            )
            return playlist_name
        if isinstance(value, str) and value.strip():
            return value.strip()
        return playlist_name

    def on_created(self, playlist, playlist_name):
        if playlist is None:
            return
        fn = getattr(self._backend, 'on_created', None)
        if not callable(fn):
            return
        try:
            fn(playlist, playlist_name)
        except Exception:
            logger.exception(
                "Plugin %s: on_created hook failed for %r",
                self.owner, playlist_name,
            )

    def after_cleanup(self, deleted_ids, playlists):
        fn = getattr(self._backend, 'after_cleanup', None)
        if not callable(fn):
            return
        try:
            fn(deleted_ids, playlists)
        except Exception:
            logger.exception("Plugin %s: after_cleanup hook failed", self.owner)

    def assign_chunks(self, final_songs, max_songs, final_name, base_name, final_playlists):
        if max_songs > 0 and len(final_songs) > max_songs:
            for offset in range(0, len(final_songs), max_songs):
                idx = offset // max_songs + 1
                chunk_name = self.format_name(final_name, chunk_index=idx)
                final_playlists[chunk_name] = final_songs[offset : offset + max_songs]
        else:
            final_playlists[base_name] = final_songs


_DEFAULT_NAMER = _DefaultPlaylistNamer()


def get_playlist_namer():
    override = _plugin_namer_backend()
    if override is not None and _has_required_surface(override):
        return _PluginPlaylistNamer(override, _provider_owner())
    return _DEFAULT_NAMER


def _plugin_namer_backend():
    try:
        from plugin.manager import plugin_manager

        return plugin_manager.get_clustering_provider('playlist_naming')
    except Exception:
        return None


def _provider_owner():
    try:
        from plugin.manager import plugin_manager

        return plugin_manager.clustering_provider_owner('playlist_naming')
    except Exception:
        return None


def _has_required_surface(backend):
    missing = [
        name for name in REQUIRED_METHODS if not callable(getattr(backend, name, None))
    ]
    if not missing:
        return True
    logger.warning(
        "Plugin %s: its playlist_naming provider %r is missing %s; "
        "using the built-in _automatic naming instead",
        _provider_owner() or 'unknown', backend, ', '.join(missing),
    )
    return False
