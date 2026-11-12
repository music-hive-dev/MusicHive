Name:           musichive
Version:        1.3.0
Release:        1%{?dist}
Summary:        Simple local music player
License:        LicenseRef-Proprietary
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
cp -a main.py days.json *.svg *.png src ui assets %{buildroot}%{_datadir}/%{name}/
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
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

%changelog
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
