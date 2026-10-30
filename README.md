# AudioMuse-AI (Configurable Playlist Naming Fork)

[![Upstream Repository](https://img.shields.io/badge/Upstream-NeptuneHub%2FAudioMuse--AI-blue?logo=github)](https://github.com/NeptuneHub/AudioMuse-AI)
[![Pull Request](https://img.shields.io/badge/PR-%23953%20(Closed)-red?logo=github)](https://github.com/NeptuneHub/AudioMuse-AI/pull/953)
[![License](https://img.shields.io/github/license/neptunehub/AudioMuse-AI.svg)](https://github.com/NeptuneHub/AudioMuse-AI/blob/main/LICENSE)

This repository is a feature fork of the [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) project by [NeptuneHub](https://github.com/NeptuneHub).

It introduces **configurable prefix and suffix naming for cluster playlists** (addressing [Issue #842](https://github.com/NeptuneHub/AudioMuse-AI/issues/842) and proposed in [Pull Request #953](https://github.com/NeptuneHub/AudioMuse-AI/pull/953)).

---

## 📌 Project Status

* **Upstream Decision:** The feature was submitted as [PR #953](https://github.com/NeptuneHub/AudioMuse-AI/pull/953). The upstream maintainer declined merging this feature into the core codebase, stating that such customizations should be implemented via AudioMuse-AI's plugin system instead.
* **Why not a plugin?** The current AudioMuse-AI plugin API does not expose hooks for clustering or playlist generation. Implementing this as a plugin currently requires runtime monkey-patching of internal worker functions, which is fragile across core updates and breaks web UI synchronization (e.g. the "Fetch Playlists" endpoint).
* **Current state:** Public testing has concluded and the PR is closed. This fork is maintained for personal homelab use and reference.

---

## 🎯 Feature Overview

In upstream AudioMuse-AI, generated cluster playlists have a hardcoded `_automatic` suffix (e.g. `Rock_automatic`). During cleanup (`CLUSTERING_CLEANING=true`), AudioMuse-AI removes existing playlists by checking if their name strictly ends with `_automatic`.

This fork introduces two configurable parameters:
* **`CLUSTERING_PLAYLIST_PREFIX`** (default: `""`): prepended to generated playlist titles.
* **`CLUSTERING_PLAYLIST_SUFFIX`** (default: `"_automatic"`): appended to generated playlist titles.

### Key Capabilities:
* **Folder Hierarchy:** Organize playlists under virtual media server folders (e.g. prefix `Audiomuse/` creates playlists like `Audiomuse/Rock`, supported by Navidrome, Jellyfin, and Subsonic clients).
* **Custom Tags:** Customize naming tags (e.g. prefix `[AM] ` or suffix ` [Mix]`).
* **Optional Suffix:** Clear the suffix (`CLUSTERING_PLAYLIST_SUFFIX=""`) when using a prefix folder.
* **Failsafe Deletion Guard:** Automatic cleanup only deletes playlists matching your configured prefix **and** suffix. If both are left empty, deletion safely aborts to protect your playlist library.
* **100% Backward Compatible:** Default configuration preserves identical upstream behavior.

### Configuration Example (`env` / `app.env`)

```ini
# Organize cluster playlists inside an "Audiomuse/" folder without suffix:
CLUSTERING_PLAYLIST_PREFIX=Audiomuse/
CLUSTERING_PLAYLIST_SUFFIX=

# Or keep playlists in root with a custom suffix:
# CLUSTERING_PLAYLIST_PREFIX=
# CLUSTERING_PLAYLIST_SUFFIX= [AI Mix]
```

---

## 📖 Official Documentation & Upstream Project

For all official features, full documentation, media server setup guides (Navidrome, Jellyfin, LMS, Lyrion, Emby, Plex), and donations:

* 👉 **Main Repository:** [NeptuneHub/AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI)
* 👉 **Official Documentation:** [neptunehub.github.io/AudioMuse-AI](https://neptunehub.github.io/AudioMuse-AI/)
