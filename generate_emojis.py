"""Generate square Wordle letter tiles for Matrix emoji uploads."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUTPUT_DIR = Path("./emojis")
TILE_SIZE = 128
FONT_SIZE = 86
FONT_PATH = Path("./SFNS.ttf")

COLORS = {
    "gray": "#5f6365",
    "yellow": "#c9b458",
    "green": "#6aaa64",
}


def load_font() -> ImageFont.FreeTypeFont:
    if FONT_PATH.exists():
        return ImageFont.truetype(str(FONT_PATH), FONT_SIZE)
    return ImageFont.load_default()


def generate_emojis() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    font = load_font()

    Image.new("RGB", (TILE_SIZE, TILE_SIZE), COLORS["gray"]).save(
        OUTPUT_DIR / "empty_gray.png"
    )

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for state, background in COLORS.items():
            image = Image.new("RGB", (TILE_SIZE, TILE_SIZE), background)
            draw = ImageDraw.Draw(image)
            bounds = draw.textbbox((0, 0), letter, font=font)
            text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]
            position = (
                (TILE_SIZE - text_width) / 2 - bounds[0],
                (TILE_SIZE - text_height) / 2 - bounds[1],
            )
            draw.text(position, letter, font=font, fill="white")
            image.save(OUTPUT_DIR / f"{letter}_{state}.png")


if __name__ == "__main__":
    generate_emojis()
