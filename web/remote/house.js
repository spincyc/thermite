// Generated remote model; edit the house file, not this preview.
globalThis.REMOTE_HOUSE = {
  "version": 1,
  "rooms": [
    {
      "id": "example_west",
      "label": "Example West",
      "sources": [
        "example_player",
        "example_computer"
      ],
      "display": "example_tv",
      "audio": "example_tv",
      "devices": [
        "example_tv",
        "example_player",
        "example_matrix"
      ],
      "routeProblem": "loc_example_west_source_problem"
    },
    {
      "id": "example_east",
      "label": "Example East",
      "sources": [
        "example_computer"
      ],
      "display": "example_display",
      "audio": "example_display",
      "devices": [
        "example_display",
        "example_computer"
      ],
      "routeProblem": "loc_example_east_source_problem"
    }
  ],
  "sources": {
    "example_player": {
      "label": "Player",
      "kind": "streamer",
      "model": "Example Player",
      "remote": "apple_tv"
    },
    "example_computer": {
      "label": "Computer",
      "kind": "computer",
      "model": "Computer",
      "remote": "pc"
    }
  },
  "devices": {
    "example_tv": {
      "label": "Example Tv",
      "short": "Example TV",
      "kind": "tv",
      "model": "Example TV",
      "remote": "sony_tv",
      "control": {
        "key": "example_tv_key",
        "power": "example_tv_power",
        "mute": "example_tv_mute",
        "volume": {
          "item": "example_tv_volume",
          "max": 40,
          "step": 1,
          "unit": "",
          "offset": 0
        }
      },
      "problem": "example_tv_problem"
    },
    "example_player": {
      "label": "Example Player",
      "short": "Example Player",
      "kind": "streamer",
      "model": "Example Player",
      "remote": "apple_tv",
      "control": {
        "key": "example_player_key",
        "power": "example_player_power"
      }
    },
    "example_display": {
      "label": "Example Display",
      "short": "Example Display",
      "kind": "tv",
      "model": "Example Display",
      "remote": "sony_tv",
      "control": {
        "key": "example_display_key",
        "power": "example_display_power",
        "mute": "example_display_mute",
        "volume": {
          "item": "example_display_volume",
          "max": 40,
          "step": 1,
          "unit": "",
          "offset": 0
        }
      },
      "problem": "example_display_problem"
    },
    "example_computer": {
      "label": "Example Computer",
      "short": "Computer",
      "kind": "computer",
      "model": "Computer",
      "remote": "pc"
    },
    "example_matrix": {
      "label": "Example Matrix",
      "short": "HDMI matrix",
      "kind": "hdmi_matrix",
      "model": "HDMI matrix"
    }
  },
  "initial": {}
};
