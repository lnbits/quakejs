# Public arena share artwork

`template.png` was created using built-in image generation in edit mode from the
Quake screenshot supplied by the extension owner. It retains the screenshot's
branding. The owner-supplied image is promotional artwork, not bundled game data.

Exact generation prompt:

> Edit the attached Quake gameplay screenshot into a social share card template. Keep the screenshot background, scene, colors, weapon, HUD and corner branding faithfully unchanged. Wide landscape image, ideally 1280 by 720. Add exactly two lines of very large bold white uppercase condensed sans serif text, centered horizontally: first line 'FIGHT ME IN QUAKE', second line 'SATS FOR KILLS'. Place these two lines in the middle area, with enough space for an equally bold third line underneath around y=490 of a 720px canvas. Strong dark text outline/shadow for clear contrast against the red screenshot. Do NOT add the third line: the extension will render the arena-specific price there programmatically. No invented price, no placeholder X, no other added text. Preserve the supplied image composition.

`../../share.py` renders the final 1200 × 675 JPEG, adding the third line with the
arena's stored price using the existing Pillow dependency. No image generation
service is called at runtime. DejaVu Sans Condensed Bold 2.37 is bundled for
consistent typography; its embedded copyright and license notices are reproduced
in `FONT-LICENSE.txt`.
