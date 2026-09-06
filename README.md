# Matrix Wordle Bot

A Matrix bot that plays the current New York Times Wordle. Games are keyed by
Matrix room ID, so each DM has its own game while everyone in a shared room
plays together.

Create a bot user and put its credentials in `.env`:

```text
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_USER=@wordlebot:example.org
MATRIX_PASSWORD=...
MATRIX_DEVICE_NAME=wordle-bot
WORDLE_SHARE_ROOM_ALIAS=#general:dendrite.fabianmild.dev
```

Invite the bot to a room and use:

- `!guess ABCDE` starts that room's game if necessary and submits a five-letter
  guess. Guesses must be in the accepted NYT Wordle vocabulary; green, yellow,
  and gray squares show the result; six guesses are allowed.
- `!share` reposts the completed result. An optional comment can be added, for
  example `!share This Wordle was difficult today`.
- After a game ends, the bot automatically posts the completed 5x6 grid and
  winner message to `#general:dendrite.fabianmild.dev`. `!share` can repost it.
  The grid uses the generated Matrix emoji names, including `:empty_gray:`.
  Set `WORDLE_SHARE_ROOM_ALIAS` to change the destination.

The answer is fetched from the NYT Wordle endpoint and cached for the current
UTC day. The accepted-guess vocabulary is fetched once and cached for the
process.

To regenerate the 26 × 3 square letter emoji tiles, run:

```sh
uv run python generate_emojis.py
```

The 79 PNG files are written to `./emojis`. On startup, the bot uploads these
assets to the Matrix media repository and uses their `mxc://` URLs in the
formatted message, with shortcode text retained as a fallback for clients
that do not render custom emoji.
