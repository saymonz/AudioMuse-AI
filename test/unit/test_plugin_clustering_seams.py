# AudioMuse-AI - https://github.com/NeptuneHub/AudioMuse-AI
# Copyright (C) 2025 NeptuneHub
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root or <https://github.com/NeptuneHub/AudioMuse-AI/blob/main/LICENSE>

"""Unit tests for the clustering plugin seam (playlist_naming).

Covers ``PluginContext.register_clustering_provider``,
``PluginManager.get_clustering_provider``, ``tasks._playlist_namer.get_playlist_namer``,
and the clustering / mediaserver callers that consume it:
``_previous_names_for_naming``, ``_name_and_prepare_playlists``, and
``delete_playlists_where``.

Main Features:
* PluginContext registers clustering components and stores factory and cache opts
* PluginManager resolves and caches clustering providers, falling back on errors
* get_playlist_namer resolves the provider or falls back to the default namer
* Isolates per-call errors and colliding names so duplicate names fall back
* Clustering and mediaserver delete_playlists_where delegate cleanup cleanly
"""

import types
from unittest.mock import patch

import plugin.api as api
import plugin.manager as manager
import tasks._playlist_namer as playlist_namer
from tasks import clustering


def _record(plugin_id, load_status='ok', clustering_providers=None):
    return {
        'id': plugin_id,
        'name': plugin_id,
        'version': '1.0.0',
        'manifest': {},
        'checksum': 'x',
        'requirements': [],
        'enabled': True,
        'settings': {},
        'source_repo': None,
        'load_status': load_status,
        'menu_items': [],
        'cron_tasks': {},
        'onnx_providers': [],
        'analysis_providers': {},
        'clustering_providers': clustering_providers or {},
        'error': None,
    }


class _FolderNamer:
    def __init__(self, prefix='Audiomuse/', suffix=''):
        self.prefix = prefix
        self.suffix = suffix
        self.created = []
        self.cleaned = []

    def format_name(self, base_name, chunk_index=None):
        stem = f"{base_name} ({chunk_index})" if chunk_index is not None else base_name
        return f"{self.prefix}{stem}{self.suffix}"

    def should_delete(self, playlist):
        name = (playlist or {}).get('Name') or ''
        if not self.prefix and not self.suffix:
            return False
        return name.startswith(self.prefix) and name.endswith(self.suffix)

    def unformat_name(self, playlist_name):
        if not isinstance(playlist_name, str):
            return None
        if self.prefix and not playlist_name.startswith(self.prefix):
            return None
        if self.suffix and not playlist_name.endswith(self.suffix):
            return None
        end = len(playlist_name) - len(self.suffix) if self.suffix else len(playlist_name)
        return playlist_name[len(self.prefix):end]

    def on_created(self, playlist, playlist_name):
        self.created.append((playlist, playlist_name))

    def after_cleanup(self, deleted_ids, playlists):
        self.cleaned.append((list(deleted_ids), len(playlists)))


class TestRegisterClusteringProvider:
    def test_context_stores_factory(self):
        ctx = api.PluginContext('demo', 'worker')
        backend = object()
        ctx.register_clustering_provider('playlist_naming', backend)
        assert ctx.clustering_providers == {
            'playlist_naming': {'factory': backend, 'cache': True}
        }

    def test_cache_opt_out_is_stored(self):
        ctx = api.PluginContext('demo', 'worker')
        ctx.register_clustering_provider('playlist_naming', object(), cache=False)
        assert ctx.clustering_providers['playlist_naming']['cache'] is False

    def test_unknown_component_is_ignored(self, caplog):
        ctx = api.PluginContext('demo', 'worker')
        ctx.register_clustering_provider('unknown_step', object())
        assert ctx.clustering_providers == {}
        assert 'unknown component' in caplog.text

    def test_double_registration_warns_and_keeps_last(self, caplog):
        ctx = api.PluginContext('demo', 'worker')
        first, second = object(), object()
        ctx.register_clustering_provider('playlist_naming', first)
        ctx.register_clustering_provider('playlist_naming', second)
        assert ctx.clustering_providers['playlist_naming']['factory'] is second
        assert 'two clustering providers' in caplog.text

    def test_two_plugins_for_one_component_warn(self, caplog):
        mgr = manager.PluginManager()
        first, second = object(), object()
        mgr.records = {
            'a': _record('a', clustering_providers={'playlist_naming': first}),
            'b': _record('b', clustering_providers={'playlist_naming': second}),
        }
        assert mgr.get_clustering_provider('playlist_naming') is first
        assert mgr.clustering_provider_owner('playlist_naming') == 'a'
        assert 'all provide the clustering component' in caplog.text

    def test_cached_and_uncached_factories(self):
        mgr = manager.PluginManager()
        calls = []

        def factory():
            calls.append(1)
            return object()

        mgr.records = {
            'p': _record('p', clustering_providers={
                'playlist_naming': {'factory': factory, 'cache': True},
            })
        }
        assert (
            mgr.get_clustering_provider('playlist_naming')
            is mgr.get_clustering_provider('playlist_naming')
        )
        assert len(calls) == 1

        mgr_uncached = manager.PluginManager()
        mgr_uncached.records = {
            'p': _record('p', clustering_providers={
                'playlist_naming': {'factory': object, 'cache': False},
            })
        }
        assert (
            mgr_uncached.get_clustering_provider('playlist_naming')
            is not mgr_uncached.get_clustering_provider('playlist_naming')
        )

    def test_sync_clears_clustering_cache_and_owners(self, monkeypatch):
        mgr = manager.PluginManager()
        mgr._clustering_provider_cache = {'playlist_naming': object()}
        mgr._clustering_provider_owners = {'playlist_naming': 'p'}
        monkeypatch.setattr(mgr, 'enabled', lambda: False)
        mgr.sync()
        assert mgr._clustering_provider_cache == {}
        assert mgr._clustering_provider_owners == {}

    def test_broken_or_none_factory_falls_back(self, caplog):
        mgr = manager.PluginManager()

        def boom():
            raise RuntimeError('broken namer')

        fallback = object()
        mgr.records = {
            'bad': _record('bad', clustering_providers={'playlist_naming': boom}),
            'idle': _record('idle', clustering_providers={'playlist_naming': lambda: None}),
            'good': _record('good', clustering_providers={'playlist_naming': fallback}),
        }
        assert mgr.get_clustering_provider('playlist_naming') is fallback
        assert mgr.clustering_provider_owner('playlist_naming') == 'good'
        assert 'bad' in caplog.text
        assert 'returned None' in caplog.text


class TestPlaylistNamerResolver:
    def test_returns_default_when_no_plugin_is_registered(self, monkeypatch):
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: None
        )
        namer = playlist_namer.get_playlist_namer()
        assert namer is playlist_namer._DEFAULT_NAMER
        assert namer.format_name('Midnight Jazz') == 'Midnight Jazz_automatic'
        assert namer.format_name('Midnight Jazz', chunk_index=2) == 'Midnight Jazz_automatic (2)'
        assert namer.should_delete({'Name': 'Midnight Jazz_automatic'}) is True
        assert namer.should_delete({'Name': 'Midnight Jazz'}) is False
        assert namer.unformat_name('Midnight Jazz_automatic') == 'Midnight Jazz_automatic'

    def test_incomplete_provider_warns_and_falls_back_to_default(self, monkeypatch, caplog):
        incomplete = types.SimpleNamespace(format_name=lambda n: n)
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: incomplete
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'half_plugin'
        )
        namer = playlist_namer.get_playlist_namer()
        assert namer is playlist_namer._DEFAULT_NAMER
        assert 'half_plugin' in caplog.text
        assert 'should_delete' in caplog.text

    def test_colliding_format_name_falls_back_instead_of_merging(self, monkeypatch, caplog):
        constant = types.SimpleNamespace(
            format_name=lambda name, chunk_index=None: 'Same Name',
            should_delete=lambda _p: False,
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: constant
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'constant_plugin'
        )
        namer = playlist_namer.get_playlist_namer()
        assert namer.format_name('Rock') == 'Same Name'
        assert namer.format_name('Jazz') == 'Jazz_automatic'
        assert namer.format_name('Jazz', chunk_index=2) == 'Jazz_automatic (2)'
        assert 'already used' in caplog.text

    def test_empty_or_failing_format_name_falls_back_per_call(self, monkeypatch, caplog):
        flaky = types.SimpleNamespace(
            format_name=lambda name, chunk_index=None: (
                '' if name == 'Empty' else (_ for _ in ()).throw(RuntimeError('boom'))
            ),
            should_delete=lambda _p: False,
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: flaky
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'flaky_plugin'
        )
        namer = playlist_namer.get_playlist_namer()
        assert namer.format_name('Empty') == 'Empty_automatic'
        assert namer.format_name('Crash', chunk_index=3) == 'Crash_automatic (3)'
        assert 'flaky_plugin' in caplog.text

    def test_should_delete_and_optional_hooks_swallow_exceptions(self, monkeypatch, caplog):
        def bad_hook(*_a, **_kw):
            raise RuntimeError('hook exploded')

        broken = types.SimpleNamespace(
            format_name=lambda name, chunk_index=None: f"Mix/{name}",
            should_delete=bad_hook,
            unformat_name=bad_hook,
            on_created=bad_hook,
            after_cleanup=bad_hook,
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: broken
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'broken_plugin'
        )
        namer = playlist_namer.get_playlist_namer()
        assert namer.should_delete({'Id': 'p1', 'Name': 'Mix/Rock'}) is False
        assert namer.unformat_name('Mix/Rock') == 'Mix/Rock'
        namer.on_created({'Id': 'p1'}, 'Mix/Rock')
        namer.after_cleanup(['p1'], [])
        assert 'broken_plugin' in caplog.text


class TestClusteringAndMediaServerIntegration:
    def test_previous_names_unformats_custom_affixes_while_keeping_legacy_ones(
        self, monkeypatch
    ):
        backend = _FolderNamer(prefix='Audiomuse/', suffix='_mix')
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: backend
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'playlist_naming'
        )
        monkeypatch.setattr(clustering, 'CLUSTER_NAMING_AI_HISTORY', True)
        monkeypatch.setattr(
            clustering,
            'get_recent_playlist_names',
            lambda _sid, limit: [
                'Audiomuse/Velvet Jazz_mix',
                'Old Pop_automatic',
            ],
        )

        cleaned = clustering._previous_names_for_naming('srv-1')
        assert cleaned == ['Velvet Jazz', 'Old Pop_automatic']

    def test_name_and_prepare_playlists_formats_single_and_chunked_names(self, monkeypatch):
        backend = _FolderNamer(prefix='Audiomuse/', suffix='_mix')
        monkeypatch.setattr(
            manager.plugin_manager, 'get_clustering_provider', lambda _c: backend
        )
        monkeypatch.setattr(
            manager.plugin_manager, 'clustering_provider_owner', lambda _c: 'playlist_naming'
        )

        result = clustering._name_and_prepare_playlists(
            {
                'named_playlists': {
                    'Small': [('s1', 'T1', 'A1')],
                    'Large': [('s2', 'T2', 'A2'), ('s3', 'T3', 'A3'), ('s4', 'T4', 'A4')],
                },
                'playlist_centroids': {},
                'playlist_primary_genres': {},
                'parameters': {'max_songs_per_cluster': 2},
            },
            'NONE', '', '', '', '', '', '', '', '', '',
        )

        assert sorted(result.keys()) == [
            'Audiomuse/Large (1)_mix',
            'Audiomuse/Large (2)_mix',
            'Audiomuse/Small_mix',
        ]
        assert all(backend.should_delete({'Name': k}) for k in result)

    @patch('tasks.mediaserver.config')
    @patch('tasks.mediaserver.navidrome.get_all_playlists')
    @patch('tasks.mediaserver.navidrome.delete_playlist')
    def test_delete_playlists_where_uses_the_plugin_predicate(
        self, mock_delete, mock_get, mock_config
    ):
        from tasks.mediaserver import delete_playlists_where

        backend = _FolderNamer(prefix='Audiomuse/', suffix='')
        mock_config.MEDIASERVER_TYPE = 'navidrome'
        listing = [
            {'Id': 'p1', 'Name': 'Audiomuse/Velvet Jazz'},
            {'Id': 'p2', 'Name': 'Personal Favorites'},
            {'id': 'p3', 'Name': 'Audiomuse/Synthwave (1)'},
        ]
        mock_get.return_value = listing
        mock_delete.return_value = True

        deleted_ids, playlists = delete_playlists_where(backend.should_delete)

        assert deleted_ids == ['p1', 'p3']
        assert [call.args[0] for call in mock_delete.call_args_list] == ['p1', 'p3']
        assert playlists == listing
