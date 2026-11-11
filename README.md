<p align="center">
  <img src="musichive_colour.svg" alt="MusicHive logo" width="128">
</p>

<h1 align="center">MusicHive</h1>

<p align="center">A desktop music player for people who still keep their music in folders.</p>

MusicHive plays your local library on Linux. It has the things I wanted from a
music player: proper playlists, lyrics that follow the song, a real equaliser,
Last.fm scrobbling and syncing to a portable player (my beloved iPod lol). It's written in Python with Qt for the interface and GStreamer for the sound, and I run it on Fedora KDE
Plasma.

## What it does

### Your library

- Keep several libraries, each one pointing at its own folder, and switch between them from the sidebar.
- Plays MP3, FLAC, Ogg and WAV files.
- Browse by song, playlist, artist or album. The filter list next to each table can also group by album artist, genre, year or decade.
- Choose which tags the tables show, in what order, including any custom tag found in your files.
- Rate songs with stars. Ratings are written to the files, so they aren't locked inside the app.
- A History tab shows what you've been listening to.
- Artist pictures and descriptions are fetched from the internet and kept on disk.

### Playback

- Shuffle, repeat one or repeat all, and a queue with Play next and Add to queue.
- An Upcoming list that shows exactly what's coming, where you can jump ahead or remove songs.
- Stop after a chosen song.
- Crossfade between songs, optionally also when you skip.
- Soft fades when pausing or stopping.
- Skips silence at the start and end of songs.
- Picks up where you left off, paused at the same spot in the same song.
- Scroll over the volume icon to change the volume by 1%, or 10% with Shift held.

### Sound

- 10 or 15 band equaliser with a pre-amp, built-in presets and your own saved ones.
- Left and right balance.
- Level Volume measures how loud your songs are and stores the result as standard ReplayGain tags, so a whole library plays at one volume and other players can use the numbers too.

### Now Playing

- A live spectrum visualiser.
- Timed lyrics in LRC format, read from your files. The current line is highlighted and centred, and plain lyrics work too.
- Scrolling moves one line per wheel click. After you scroll, the lyrics stay put for a few seconds before following the song again.

### Mini player

A small borderless window for when the main one is in the way. It shows the
title and artist with previous, play/pause and next buttons, a seek bar and a
volume icon you can scroll over or click to mute. The visualiser runs behind it
if you want it to, and right-clicking gives you shuffle, repeat and a way back to
the main window. It stays on top, stays out of the taskbar and remembers where
you left it.

On Wayland, those last three things depend on KDE's window manager. On other
Wayland desktops the mini player still works, but it won't stay on top or keep
its position.

### Playlists

- Normal playlists with their own image and description. Animated GIFs work as playlist images.
- Auto-playlists that fill themselves from rules such as "Genre contains Rock" or "Year is greater than 1999".
- Import and export M3U and M3U8 files.
- Drag songs between lists. Hold a song over a tab and that tab opens.

### Tags and file tools

- A tag editor for one song or many at once. When editing several songs, a field they disagree on stays empty and is only written if you type in it, so nothing changes by accident.
- Find album covers and genres online. Nothing is saved until you've approved it in a side by side comparison.
- Auto-Tag Genres does the same for a whole set of songs.
- Organise Files moves a library into a folder structure from a naming template like `<albumartist>/<album>/<trackno> - <title>`.
- Convert Files makes copies in MP3, Ogg Vorbis, Opus or FLAC.

### Devices

Plug in a USB drive, an iPod or anything else that shows up as storage and
MusicHive can sync a library to it. It can keep the original files or convert
them with a quality profile, rename folders with a template, shrink embedded
artwork and copy your playlists across. Before it deletes anything from the
device, it shows you exactly which files would go.

Rockbox players get some extra attention: the device details show up in the
Devices tab, and the plays in a `.scrobbler.log` can be sent to Last.fm. Last.fm
limits how many scrobbles it accepts in a day, so a big log goes out a day's worth
at a time.

### Connected to your desktop

- Last.fm scrobbling, now playing updates, and downloading your play counts.
- Discord Rich Presence with the album cover.
- MPRIS support, so desktop widgets and media keys can see and control it.
- A tray icon, minimizing to the tray and starting hidden in the tray.

### Small things

- Settings can be exported to a file and imported again.
- The data folder can be moved somewhere else.
- Some small easter eggs for you to find. :)

## Installing

MusicHive is packaged as an RPM for Fedora. Build it from a clone of this repository:

```
sudo dnf install rpm-build python3-devel
./packaging/build-rpm.sh
```

The package ends up in `dist/`, built from the latest commit. Install it with:

```
sudo dnf install ./dist/musichive-*.noarch.rpm
```

`dnf` brings in everything else it needs, and MusicHive then shows up in your
application menu. The package is built for the Fedora release you build it on.

### Debian and Ubuntu

The `.deb` needs `dpkg-deb`, which Debian-based systems already have (on Fedora,
install `dpkg`):

```
./packaging/build-deb.sh
sudo apt install ./dist/musichive_*_all.deb
```

It needs a release that packages PySide6, such as Debian 13 or Ubuntu 24.10 and
later.

### Arch Linux

On Arch the script uses `makepkg`. On any other distribution it builds inside an
Arch Linux container, so you only need `podman`:

```
./packaging/build-arch.sh
sudo pacman -U ./dist/musichive-*-any.pkg.tar.zst
```

### AppImage

The AppImage carries its own Python, Qt and GStreamer, so it runs without
installing anything. It's built inside a Debian 12 container with `podman`:

```
./packaging/build-appimage.sh
./dist/MusicHive-*-x86_64.AppImage
```

It runs on 64-bit PCs with a distribution at least as new as Debian 12 (Fedora
37, Ubuntu 23.04 and Arch all qualify). Unlike the packages, it doesn't add
MusicHive to your application menu by itself.

## Running from source

On Fedora, the libraries are all available as system packages:

```
sudo dnf install python3-pyside6 python3-mutagen python3-gobject \
    gstreamer1-plugins-good gstreamer1-plugins-bad-free rsms-inter-fonts
python3 main.py
```

On another distribution you'll need the equivalent packages. Python 3 with
PySide6, Mutagen and PyGObject, plus GStreamer and its good plugins, should be
enough. The interface uses the Inter font if it's installed.

## Last.fm setup

Last.fm needs an API key, which isn't included in this repository. Get one from
Last.fm, then put it in `~/Music/.musichive/secrets`:

```json
{
  "lastfm_api_key": "your key",
  "lastfm_api_secret": "your secret"
}
```

Restart MusicHive and log in from the Last.fm window.

## Where your data lives

Libraries, playlists and cached images are kept in `~/Music/.musichive`, unless
you've moved that folder in the settings. Other settings are in
`~/.config/MusicHive`. Uninstalling the app leaves both of them alone.

MusicHive is a local player, but a few features do use the internet. Artist
pictures and album searches go to Deezer and iTunes, and Last.fm gets your plays
if you've turned scrobbling on. Discord can only show images that are online, so
with Rich Presence on, the cover of the current song is uploaded to
[Litterbox](https://litterbox.catbox.moe), a temporary file host.

## Not finished yet

The Radio and Podcasts tabs are there, but they're empty for now.

## Built with

- [PySide6](https://doc.qt.io/qtforpython-6/) (Qt for Python), LGPL-3.0
- [GStreamer](https://gstreamer.freedesktop.org/), LGPL-2.1
- [PyGObject](https://pygobject.gnome.org/), LGPL-2.1
- [Mutagen](https://mutagen.readthedocs.io/), GPL-2.0-or-later
- Icons from [Lucide](https://lucide.dev/), ISC License, some derived from Feather, MIT License
- Emoji from [Twemoji](https://github.com/twitter/twemoji), CC BY 4.0, in the Twemoji Mozilla font
