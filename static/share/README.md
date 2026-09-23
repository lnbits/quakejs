# Public arena share artwork

`template.png` was created using built-in image generation in edit mode from the
replacement Quake screenshot supplied by the extension owner. The owner-supplied
image is promotional artwork, not bundled game data.

Latest edit prompt (built-in image generation, using the replacement screenshot):

> Use case: text-localization. Edit target: the newest attached image, the 1280x720 Quake gameplay screenshot with a central red enemy and muzzle flash. Create a replacement social-share thumbnail template using THIS screenshot as the full background. Preserve its scene, architecture, action, weapon, HUD, colors, framing and landscape 16:9 aspect ratio as faithfully as possible. Add exactly two centered lines of very large bold condensed WHITE uppercase text with strong black outline/shadow for contrast: first line "JOIN THE BATTLE", second line "SATS FOR KILLS". Match classic bold game thumbnail typography. Position first line around y=245 and second line around y=390 on the 1280x720 canvas, with sufficient clear space for the application to later draw a third price line centered at y=533. Do not add a price, placeholder, third line, logos or watermarks. The background must be the supplied new gameplay screenshot, not the earlier screenshot with corner branding. Output the edited template.

`../../share.py` renders the final 1200 × 675 JPEG, adding the third line with the
arena's stored price using the existing Pillow dependency. No image generation
service is called at runtime. DejaVu Sans Condensed Bold 2.37 is bundled for
consistent typography; its embedded copyright and license notices are reproduced
in `FONT-LICENSE.txt`.

`lobby.png` is a static public-lobby card made with the imagegen skill using
the earlier branded template as the edit target. Its two centered bold white lines read
"CREATE QUAKE MATCHES" and "AND CHARGE A JOIN FEE", replacing the original
promotional lettering while preserving the screenshot and its corner branding.
The lobby includes server-rendered Open Graph and Twitter large-image metadata;
no JavaScript, account, wallet key or image-generation service is needed to fetch
the public preview.
