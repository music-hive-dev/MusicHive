// Run by KWin when the mini player opens (see src/mini_player.py). On Wayland a
// program cannot keep its own window on top, place it, or see where it is.
// The title must match the windowTitle in ui/mini_player.ui.
var title = "MusicHive Mini Player";
var saved = __SAVED_POSITION__; // filled in by mini_player.py

function isOnAScreen(x, y) {
    var screens = workspace.screens || [];
    for (var i = 0; i < screens.length; i++) {
        var area = screens[i].geometry;
        if (x >= area.x && y >= area.y && x < area.x + area.width && y < area.y + area.height) {
            return true;
        }
    }
    return false; // its monitor may have been unplugged since
}

function setUp(miniPlayer) {
    miniPlayer.keepAbove = true;
    miniPlayer.skipTaskbar = true;
    if (saved && isOnAScreen(saved.x, saved.y)) {
        miniPlayer.frameGeometry = {
            x: saved.x,
            y: saved.y,
            width: miniPlayer.width,
            height: miniPlayer.height
        };
    }
    miniPlayer.interactiveMoveResizeFinished.connect(function () {
        var place = miniPlayer.frameGeometry;
        var position = Math.round(place.x) + "," + Math.round(place.y);
        callDBus("org.musichive.MiniPlayer", "/", "", "savePosition", position);
    });
}

var windows = workspace.windowList ? workspace.windowList() : workspace.clientList();
for (var i = 0; i < windows.length; i++) {
    if (windows[i].caption === title) {
        setUp(windows[i]);
    }
}

// The window usually reaches KWin only after this script has started.
var windowAdded = workspace.windowAdded || workspace.clientAdded;
windowAdded.connect(function (added) {
    if (added.caption === title) {
        setUp(added);
    }
});
