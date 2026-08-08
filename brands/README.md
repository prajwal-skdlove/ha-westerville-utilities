# Icon assets

`custom_integrations/westerville_utilities/` mirrors the layout expected by
the [home-assistant/brands](https://github.com/home-assistant/brands)
repository (`custom_integrations/<domain>/icon.png` + `icon@2x.png`,
256x256 / 512x512, PNG with alpha).

This is an original mark: a "W" monogram with a lightning-bolt notch worked
into its middle stroke, on a navy-to-teal badge. It does not reuse any City
of Westerville or Westerville Electric Division artwork, colors, or
wordmark.

`make_icon.py` regenerates both PNGs from scratch (pure PIL, no external
SVG assets) -- tweak the constants at the top (colors, kink_amount, stroke
width) and rerun to iterate.

## Getting it to actually show up in HA / HACS

HACS and Home Assistant's own integrations page don't read icons from this
repo directly -- they look them up by domain (`westerville_utilities`) in
home-assistant/brands. To wire it up:

1. Fork `home-assistant/brands`.
2. Add this folder's contents under `custom_integrations/westerville_utilities/`.
3. Open a PR against home-assistant/brands.

Once merged, the icon appears automatically in HACS and in HA's
integrations UI -- no changes needed on this repo's side.
