/*
 * The remotes the page draws: one for each kind of device it controls, with
 * the buttons that device's own remote has. house.js names each device's
 * remote (and each source's). Every remote is drawn in the same frame, so the
 * D-pad and the volume stay where the thumb expects them.
 *
 * REMOTE_LAYOUTS = {
 *   version: 1,
 *   buttons: { <id>: { label, icon, text, repeat } }
 *       every button any remote has. icon names a symbol in index.html
 *       (#i-<icon>); text is a caption instead; repeat: held, it presses again
 *   remotes: { <id>: {
 *       label,                       what the real remote is called
 *       corners: [tl, tr, bl, br],   the keys around the D-pad, null for none;
 *                                    absent: the remote has no D-pad
 *       top: [id, ...],              keys after the D-pad, or first without one
 *       playback: [id, ...],         up to three sit beside Mute; more get a row of their own
 *       channel: true,               a CH rocker opposite the volume
 *       keys: [id, ...],             labelled keys under the D-pad
 *       keyGroups: [{label, keys, columns, primary?}],
 *                                    optional semantic grids; keys is derived
 *                                    from these for older consumers. Secondary
 *                                    groups can sit beside a wide terminal remote.
 *       note                         instead of buttons, for a source that takes none
 *                                    (a PC); {device} becomes its name
 *   } }
 * }
 *
 * Volume and Mute are on every remote and always drive the room's audio
 * device, whichever remote is shown. Voice buttons (Siri, Google Assistant)
 * are left out: a press cannot carry speech. Sony TV keys use its IP-control
 * IRCC commands, advertised by the TV's getRemoteControllerInfo response.
 */
globalThis.REMOTE_LAYOUTS = {
  version: 1,

  buttons: {
    power: { label: "Power", icon: "power" },
    input: { label: "Input", icon: "input" },
    settings: { label: "Settings", icon: "settings" },
    menu: { label: "Menu", icon: "menu" },
    home: { label: "Home", icon: "home" },
    back: { label: "Back", icon: "back" },
    tv: { label: "TV", icon: "tv" },
    eject: { label: "Open/close", icon: "eject" },

    up: { label: "Up", icon: "up", repeat: true },
    down: { label: "Down", icon: "down", repeat: true },
    left: { label: "Left", icon: "left", repeat: true },
    right: { label: "Right", icon: "right", repeat: true },
    ok: { label: "OK", text: "OK" },

    play_pause: { label: "Play/pause", icon: "play-pause" },
    play: { label: "Play", icon: "play" },
    pause: { label: "Pause", icon: "pause" },
    stop: { label: "Stop", icon: "stop" },
    rewind: { label: "Rewind", icon: "rewind" },
    fast_forward: { label: "Fast-forward", icon: "forward" },
    previous: { label: "Previous", icon: "previous" },
    next: { label: "Next", icon: "next" },

    channel_up: { label: "Channel up", icon: "up", repeat: true },
    channel_down: { label: "Channel down", icon: "down", repeat: true },
    volume_up: { label: "Volume up", icon: "plus", repeat: true },
    volume_down: { label: "Volume down", icon: "minus", repeat: true },
    mute: { label: "Mute", icon: "mute" },

    netflix: { label: "Netflix", text: "NETFLIX" },
    youtube: { label: "YouTube", text: "YouTube" },
    top_menu: { label: "Top menu", text: "Top menu" },
    popup_menu: { label: "Pop-up menu", text: "Pop-up" },
    options: { label: "Options", text: "Options" },
    display: { label: "Display", text: "Display" },
    audio: { label: "Audio track", text: "Audio" },
    subtitle: { label: "Subtitles", text: "Subtitle" },
    info: { label: "Info", text: "Info" },
    guide: { label: "Programme guide", text: "Guide" },
    action_menu: { label: "Action menu", text: "Action menu" },
    exit: { label: "Exit", text: "Exit" },
    digit_1: { label: "1", text: "1" },
    digit_2: { label: "2", text: "2" },
    digit_3: { label: "3", text: "3" },
    digit_4: { label: "4", text: "4" },
    digit_5: { label: "5", text: "5" },
    digit_6: { label: "6", text: "6" },
    digit_7: { label: "7", text: "7" },
    digit_8: { label: "8", text: "8" },
    digit_9: { label: "9", text: "9" },
    digit_0: { label: "0", text: "0" },
    digit_dot: { label: "Channel decimal point", text: "." },
    digit_enter: { label: "Channel enter", text: "Enter" },
    red: { label: "Red", text: "Red" },
    green: { label: "Green", text: "Green" },
    yellow: { label: "Yellow", text: "Yellow" },
    blue: { label: "Blue", text: "Blue" },
    help: { label: "Help", text: "Help" },
    sound_movie: { label: "Movie sound", text: "Movie" },
    sound_music: { label: "Music sound", text: "Music" },
    sound_game: { label: "Game sound", text: "Game" },
    sound_pure: { label: "Pure sound", text: "Pure" },
    picture: { label: "Picture mode", text: "Picture" },
    aspect: { label: "Aspect", text: "Aspect" },
    lens: { label: "Picture position", text: "Position" },
    night: { label: "Night sound", text: "Night" },
    speech: { label: "Speech enhancement", text: "Speech" }
  },

  remotes: {
    nvidia_shield: {
      label: "SHIELD remote",
      corners: ["power", "menu", "back", "home"],
      playback: ["rewind", "play_pause", "fast_forward"],
      keys: ["netflix"]
    },
    apple_tv: {
      label: "Siri Remote",
      corners: ["power", null, "back", "tv"],
      playback: ["play_pause"],
      keyGroups: [{ label: "Apps", keys: ["netflix", "youtube"], columns: 2, primary: true }]
    },
    sony_bluray: {
      label: "Blu-ray remote",
      corners: ["power", "eject", "back", "home"],
      playback: ["previous", "rewind", "play", "pause", "stop", "fast_forward", "next"],
      keyGroups: [
        { label: "Apps", keys: ["netflix"], columns: 1, primary: true },
        { label: "Disc menus", keys: ["top_menu", "popup_menu", "options", "display"], columns: 2 },
        { label: "Audio", keys: ["audio", "subtitle"], columns: 2 }
      ]
    },
    sony_tv: {
      label: "Sony TV remote",
      corners: ["power", "settings", "back", "home"],
      top: ["input", "guide", "info", "action_menu"],
      playback: ["previous", "rewind", "play", "pause", "stop", "fast_forward", "next"],
      channel: true,
      keyGroups: [
        { label: "Apps", keys: ["netflix", "youtube"], columns: 2, primary: true },
        { label: "TV", keys: ["tv", "exit", "options", "help"], columns: 2 },
        { label: "Audio", keys: ["audio", "subtitle"], columns: 2 },
        { label: "Channels", keys: ["digit_1", "digit_2", "digit_3", "digit_4", "digit_5", "digit_6",
          "digit_7", "digit_8", "digit_9", "digit_dot", "digit_0", "digit_enter"], columns: 3 },
        { label: "Color keys", keys: ["red", "green", "yellow", "blue"], columns: 4 }
      ]
    },
    android_tv: {
      label: "Android TV remote",
      corners: ["power", null, "back", "home"],
      playback: ["rewind", "play", "pause", "fast_forward"],
      channel: true,
      keys: ["netflix", "youtube"]
    },
    sony_projector: {
      label: "Projector remote",
      corners: ["power", "input", "menu", null],
      keys: ["picture", "aspect", "lens"]
    },
    marantz_avr: {
      label: "Marantz remote",
      corners: ["power", "input", "back", "settings"],
      keyGroups: [
        { label: "Sound modes", keys: ["sound_movie", "sound_music", "sound_game", "sound_pure"], columns: 2 },
        { label: "Information", keys: ["info", "options"], columns: 2 }
      ]
    },
    sonos: {
      label: "Audio controls",
      playback: ["previous", "play_pause", "next"],
      keyGroups: [{ label: "Audio", keys: ["tv", "night", "speech"], columns: 3, primary: true }]
    },
    wiim: {
      label: "WiiM remote",
      top: ["input"],
      playback: ["previous", "play_pause", "next"]
    },
    pc: {
      label: "Keyboard and mouse",
      note: "{device} is a PC: use its own keyboard and mouse"
    },
    ps5: {
      label: "DualSense controller",
      note: "thermite cannot drive {device}: use its own controller"
    }
  }
};

// One definition of group membership; legacy command/content consumers still
// see the same flat keys array, with no extra button IDs or control behavior.
Object.values(globalThis.REMOTE_LAYOUTS.remotes).forEach((remote) => {
  if (remote.keyGroups) remote.keys = remote.keyGroups.flatMap((group) => group.keys);
});
