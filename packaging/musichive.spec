Name:           musichive
Version:        1.8.0
Release:        1%{?dist}
Summary:        Simple local music player
License:        GPL-3.0-or-later
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel

Requires:       python3
Requires:       python3-pyside6
Requires:       python3-mutagen
Requires:       python3-gobject
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       curl
Requires:       rsms-inter-fonts
# Volume changes take effect more smoothly with this; the app works without it.
Recommends:     gstreamer1-plugins-bad-free
# More audio formats, such as AAC.
Recommends:     gstreamer1-plugin-libav
Recommends:     gstreamer1-plugins-ugly-free
# For casting to Chromecasts (DLNA TVs and speakers work without it).
Recommends:     python3-pychromecast
# The QR code guests scan in Party Mode (they can type the address without it).
Recommends:     python3-segno
# For casting the visualiser and lyrics to a DLNA TV (H.264 video).
Recommends:     gstreamer1-plugin-openh264
# For ejecting and opening portable devices.
Recommends:     udisks2
Recommends:     dolphin

%description
MusicHive is a local music player with playlists, an equaliser, lyrics,
Last.fm scrobbling and a mini player.

%prep
%autosetup

%build
# Nothing to build: the program is Python.

%install
install -d %{buildroot}%{_datadir}/%{name}
cp -a main.py days.json *.svg *.png src ui assets translations %{buildroot}%{_datadir}/%{name}/
%py_byte_compile %{__python3} %{buildroot}%{_datadir}/%{name}

install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/musichive/main.py "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/%{name}

install -Dm644 musichive_colour.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

# The file name must match setDesktopFileName in main.py.
install -Dm644 packaging/musichive.desktop \
    %{buildroot}%{_datadir}/applications/%{name}.desktop

%files
%license LICENSE
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

%changelog
* Wed Nov 25 2026 MusicHive - 1.8.0-1
- MusicHive can be translated: choose the language in Settings > Interface, or follow the desktop's language
- German translation, by music-hive-dev
- Translations are made with Qt Linguist; translations/update.sh starts a new language and keeps them up to date
- Texts with a number now say "1 song" and "2 songs" properly in every language
- Fixed the mini player not keeping its place and staying on top when its title is translated
- The About window lists the translators

* Wed Nov 25 2026 MusicHive - 1.7.2-1
- MusicHive is now free software under the GNU General Public License, version 3 or later
- The About window is larger, and scrolls when its text does not fit

* Wed Nov 25 2026 MusicHive - 1.7.1-1
- The tray icon can be chosen in Settings: automatic, dark, light or colour
- Windows: switching the sound output moves playback to the new device straight away

* Wed Nov 25 2026 MusicHive - 1.7.0-1
- New Tools > Check Covers: lists albums whose cover is missing, unreadable, not square, too small, very large or not shared by all their songs
- From that list: edit an album's tags, search for its cover straight away, open its folder, or copy or save the list
- Shows every different cover of an album; one can be stretched to a square or used for the whole album
- Songs can be marked to skip the cover check, with a tag saved in the files
- New Tools > Check Tags: missing tags, duplicate or missing track numbers, albums whose songs disagree, and artists written more than one way; a tag can be made the same for a whole album, and tracks numbered again
- Albums can be looked up on MusicBrainz, one or all in a row, choosing which tags to take over; dates can be written as just the year
- New Tools > Find Duplicates: songs in the library more than once, with their quality side by side; keeping one moves the others to the Trash and passes on their plays, rating and places in playlists
- Edit > Tag Change History: the last 50 tag saves, each of which can be undone (and an undo undone again)
- Smart shuffle (a setting): the same artist or album no longer plays twice in a row
- Albums that play in order can keep one volume for the whole album, and can be left without a crossfade between their tracks
- The playing song and its place are saved with every song and every 30 seconds, and when the computer shuts down with MusicHive open
- The headers of playlists, artists, albums and podcasts sit in a rounded box coloured from their picture, with rounded pictures, small icon buttons and text that fits beside the picture; a playlist can have a colour of its own
- Auto-playlists show a closed lock
- No more thin seam between the visualiser's bars and their glow

* Tue Nov 24 2026 MusicHive - 1.6.11-1
- The builds on GitHub use the current versions of GitHub's actions

* Tue Nov 24 2026 MusicHive - 1.6.10-1
- Windows: Discord Rich Presence connects
- Windows: organising no longer leaves a copy of a song that is playing; it is skipped with a note
- Windows: the volume mixer remembers MusicHive's volume

* Tue Nov 24 2026 MusicHive - 1.6.9-1
- Every package is built on GitHub, and pushing a version tag makes the release

* Tue Nov 24 2026 MusicHive - 1.6.8-1
- Windows: only the GStreamer plugins and Qt parts MusicHive uses are bundled, without FFmpeg

* Tue Nov 24 2026 MusicHive - 1.6.7-1
- Windows: no longer aborts at startup when GStreamer loads FFmpeg's Whisper library twice

* Tue Nov 24 2026 MusicHive - 1.6.6-1
- Windows: MusicHive-debug.exe shows what happens at startup in a console window

* Mon Nov 23 2026 MusicHive - 1.6.5-1
- Windows: the log starts before Qt and GStreamer, and GStreamer's plugin list is kept per user

* Mon Nov 23 2026 MusicHive - 1.6.4-1
- Windows: restarting starts a fresh copy, and crashes and a stuck startup are written to the log

* Mon Nov 23 2026 MusicHive - 1.6.3-1
- D-Bus is only loaded on Linux, since the Windows build's Qt does not have it

* Mon Nov 23 2026 MusicHive - 1.6.2-1
- The Windows build installs the Qt parts MusicHive uses

* Mon Nov 23 2026 MusicHive - 1.6.1-1
- The Windows version is built after every push

* Mon Nov 23 2026 MusicHive - 1.6.0-1
- A Windows version: an installer and a portable zip, built on GitHub Actions

* Mon Nov 23 2026 MusicHive - 1.5.0-1
- Casting to Chromecasts and DLNA TVs and speakers, with previous and next on Chromecasts
- Casting with the visualiser and lyrics as a video
- Casting to a web browser on a TV, with the visualiser, lyrics and buttons for the remote
- Screenshots and the new casting features in the README

* Mon Nov 23 2026 MusicHive - 1.4.3-1
- Updated logos and requirements

* Mon Nov 23 2026 MusicHive - 1.4.2-1
- The Last.fm window can open the secrets file and explains how to set up Last.fm
- Tests for the parts that do not need the window (run with pytest)

* Thu Nov 12 2026 MusicHive - 1.4.1-1
- The visualiser can draw 60 frames a second

* Thu Nov 12 2026 MusicHive - 1.4.0-1
- Songs, albums and playlists can have their own equaliser and visualiser presets
- Visualiser presets, with a built-in Default
- Lyrics for many songs at once; instrumentals are tagged haslyrics=false
- The lyrics can take a colour from the cover, and the bars can glow
- Cover colours are remembered, so they show without delay
- Visualiser flags are JSON files, and you can add your own
- Dragging songs shows just their title and artist
- The shortcuts are in two columns, grouped by menu, and the settings tabs have icons
- Settings, Online explains how to set up Last.fm and opens the secrets file

* Thu Nov 12 2026 MusicHive - 1.3.2-1
- The visualiser can take its colours from the cover
- The lyrics can grow with the window, for big screens and TVs

* Thu Nov 12 2026 MusicHive - 1.3.1-1
- Podcasts and radio stations can be pinned to the sidebar
- The sidebar's sections can be put in another order by dragging

* Thu Nov 12 2026 MusicHive - 1.3.0-1
- Added podcasts: search, subscriptions, automatic downloads and deleting, progress
- Added internet radio with pausing back in time, taping, history and remembered songs
- Added a sleep timer
- Added settings for the visualiser, lyrics and keyboard shortcuts, and regrouped the settings
- Added type to find in lists and tables
- Auto-playlists start with every song and can use the play count
- Fixed Discord covers that stopped showing after two days

* Wed Nov 11 2026 MusicHive - 1.2.5-1
- Fixed the icon of the application menu entry

* Wed Nov 11 2026 MusicHive - 1.2.4-1
- Added a build script for an AppImage

* Wed Nov 11 2026 MusicHive - 1.2.3-1
- Added build scripts for Debian and Arch packages

* Wed Nov 11 2026 MusicHive - 1.2.2-1
- The mini player opens again after a reboot if it was open before

* Tue Nov 10 2026 MusicHive - 1.2.1-1
- The About window shows the version and a fuller description

* Tue Nov 10 2026 MusicHive - 1.2.0-1
- Added an Interface settings tab with dark and light mode
- The highlight colour can be chosen, with a default for each mode
- Offers to restart when the look is changed
- Number boxes are text fields, fixing how they were drawn
- The Last.fm window fits its contents
- Fixed a crash and a warning when quitting

* Tue Nov 10 2026 MusicHive - 1.1.1-1
- Added "Add to end of queue" to the right-click menus
- Delete removes the selected songs from the Upcoming list
- Songs can be dragged into the Upcoming list and rearranged within it

* Tue Nov 10 2026 MusicHive - 1.1-1
- Adjusted a button icon size

* Tue Nov 10 2026 MusicHive - 1.0-1
- First release
