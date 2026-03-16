#!/usr/bin/env python3
"""MAX SAFETY EBAY UK FILTER v5.7 - PARANOID MODE
Title-first decision engine. assess_title() is the single source of truth.
Default: BLOCK unless clearly boring+generic+unbranded.
"""
import csv, sys, os, re, json, argparse, unicodedata
from datetime import datetime
from collections import defaultdict, namedtuple
# ============================================================================
# DECISION TYPE
# ============================================================================
Decision = namedtuple('Decision', ['allowed', 'score', 'reasons', 'matches', 'explanation'])
# ============================================================================
# CONFIG
# ============================================================================
CONFIG = {
    'mode': 'paranoid',
    'block_threshold': 70,
    'greylist_threshold': 50,
    'allowlist_only': False,
    'detect_obfuscation': True,
    'scan_description': True,
    'max_title_length': 80,
}
# ============================================================================
# COMMON ENGLISH WORDS (for brand-like token exclusion)
# ============================================================================
COMMON_WORDS = frozenset("""
the and for with from this that your into over under about between through
after before above below along around behind beyond during without within
new old big small large mini great good best high low top hot cold warm cool
free fast easy full open close long short wide deep flat soft hard clear dark
light bright white black blue red green pink gold silver gray grey brown
purple orange yellow multi color colour
set box bag case pack kit lot pair piece unit roll sheet strip tube jar
stand holder mount rack shelf hook clip ring band belt cord wire rope cable
strap chain link loop cap lid cover wrap pad mat frame base plate block bar
plate board panel layer coat film tape patch seal lock key pin bolt nut
with pro max plus ultra super mega mini lite slim thin flex fit air dot pop
led usb rgb lcd type size mode auto self dual tri quad
portable foldable adjustable reusable removable washable waterproof
stainless steel metal plastic rubber silicone nylon cotton bamboo wood glass
ceramic leather fabric foam mesh vinyl acrylic felt paper cardboard iron
aluminum copper brass bronze titanium alloy carbon fiber chrome
universal premium quality deluxe heavy duty professional industrial commercial
home office garden kitchen bathroom bedroom living room outdoor indoor
electric digital manual automatic magnetic solar powered rechargeable wireless
creative modern vintage classic retro simple elegant cute funny custom
pcs pieces pack set kit lot bundle dozen roll pair each per unit
inch foot meter yard gram pound ounce liter gallon watt volt amp
phone tablet laptop desktop computer monitor printer scanner mouse keyboard
charging charger charge cable data sync power supply adapter converter
storage organizer container bin basket tray box drawer shelf divider
cleaning cloth sponge brush mop broom duster scrub wipe towel
plant pot planter flower garden seed watering soil ceramic terracotta
cutting chopping board knife block spoon fork spatula ladle whisk
cushion pillow cover blanket throw curtain rug mat runner tablecloth
dog cat pet collar leash bowl bed toy feeder grooming brush comb
microfiber bamboo stainless wooden plastic rubber nylon polyester
remote control sensor switch timer plug socket strip extension
lights strip fairy string rope desk lamp table reading night work
bathroom kitchen bedroom living dining outdoor indoor garage patio
wall door window ceiling floor shelf corner cabinet closet wardrobe
travel luggage suitcase backpack bag pouch carry portable foldable
medium large small extra adjustable universal standard regular
white black blue red green pink purple yellow orange brown grey beige
round square oval rectangular flat curved slim thick thin wide narrow
waterproof dustproof shockproof scratch resistant anti slip non slip
drawer tray utensil holder dispenser organizer rack hanger hook clip
towel soap toothbrush toilet bath shower mirror shelf rail bar ring
nylon velvet cotton linen silk satin polyester fleece knitted woven
""".split())
# ============================================================================
# OBFUSCATION MAP
# ============================================================================
OBFUSCATION_MAP = {
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '6': 'g', '7': 't',
    '8': 'b', '9': 'g', '@': 'a', '$': 's', '!': 'i', '+': 't',
    '\u0430': 'a', '\u0435': 'e', '\u043e': 'o', '\u0440': 'p',
    '\u0441': 'c', '\u0443': 'y', '\u0445': 'x',
    '\u0251': 'a', '\u03b5': 'e', '\u03b9': 'i', '\u03c3': 's',
    '\uff41': 'a', '\uff42': 'b', '\uff43': 'c', '\uff44': 'd', '\uff45': 'e',
}
# ============================================================================
# SAFE BRAND FIELD VALUES
# ============================================================================
SAFE_BRAND_VALUES = frozenset([
    'unbranded', 'generic', 'no brand', 'unknown', 'none', 'n/a', 'na',
    'not applicable', 'no name', 'noname', 'oem', 'other', 'various',
    'does not apply', 'not specified', '', '-',
])
# ============================================================================
# HELMET VISOR EXEMPTION
# ============================================================================
_HELMET_VISOR_PATTERN = re.compile(
    r'\b('
    r'helmet\s*visor|visor\s*lens|visor\s*shield|helmet\s*shield|'
    r'helmet\s*lens|face\s*shield|helmet\s*face|'
    r'pinlock|anti[\s\-]?fog\s*(insert|lens|visor|film)|'
    r'photochromic\s*visor|transition\s*visor|'
    r'visor\s*(for|fit|compatible|replacement)|'
    r'(for|fit|compatible|replacement)\s*.*visor|'
    r'helmet\s*screen|visor\s*screen|'
    r'iridium\s*visor|mirror\s*visor|tinted\s*visor|clear\s*visor|'
    r'dark\s*visor|smoke\s*visor|'
    r'full\s*face\s*helmet\s*lens|'
    r'(hj|hj[\s\-]?\d+|vas[\s\-]?[a-z]|cw[\s\-]?\d|cnt[\s\-]?\d|'
    r'gt[\s\-]?air|rx[\s\-]?7|nxr|rf[\s\-]?\d|x[\s\-]?spirit|'
    r'rpha|neotec|exo[\s\-]?\d|evo|qualifier|revolver|bullitt|'
    r'k[1-9]|k[1-9]s)\s*(visor|lens|shield|screen)'
    r')\b',
    re.IGNORECASE
)
_HELMET_VISOR_EXEMPT_RULES = frozenset([
    'FOR_HELMET', 'FOR_MOTO', 'FOR_AUTO',
    'AUTO_BRAND', 'MOTO_BRAND', 'VW_GROUP',
    'YAMAHA', 'HONDA', 'KAWASAKI', 'SUZUKI', 'KTM',
    'COMPAT_CLAIM', 'AUTH_CLAIM', 'AFTERMARKET',
    'COMPAT_BRAND_BLOCK', 'BRAND_TOKEN_PLUS_COMPAT',
    'HAIR_BRAND',
])
# ============================================================================
# ALLOWLIST - boring generic categories (substring match)
# ============================================================================
ALLOWLIST_CATEGORIES = [
    'storage box','storage bin','storage container','plastic container',
    'organizer box','organizer tray','drawer organizer','desk organizer',
    'basket','wicker basket','laundry basket','waste basket',
    'shelf','floating shelf','corner shelf','wall shelf',
    'rack','shoe rack','coat rack','towel rack','drying rack',
    'hanger','clothes hanger','pants hanger','coat hanger',
    'hook','wall hook','adhesive hook','door hook','robe hook',
    'divider','drawer divider','shelf divider',
    'usb cable','usb c cable','type c cable','micro usb cable',
    'charging cable','data cable','extension cord','power strip',
    'hdmi cable','ethernet cable','lan cable','aux cable','audio cable',
    'phone stand','phone holder','tablet stand','tablet holder',
    'screen protector','tempered glass','cable organizer','cable clip',
    'cable tie','cable sleeve','stylus','stylus pen',
    'pen holder','pencil holder','pencil case','desk pad','desk mat',
    'mouse pad','file folder','document folder','binder','ring binder',
    'paper clip','binder clip','rubber band','notebook','notepad',
    'sticky note','tape dispenser','stapler','hole punch',
    'whiteboard','cork board','notice board','letter tray',
    'led strip','light strip','fairy light','string light',
    'desk lamp','table lamp','reading lamp','book light','night light',
    'cabinet light','closet light','work light',
    'pet bowl','food bowl','water bowl','pet bed','dog bed','cat bed',
    'pet toy','chew toy','squeaky toy','rope toy',
    'pet collar','dog collar','cat collar','pet leash','dog leash',
    'pet brush','grooming brush','pet comb','litter scoop','litter mat',
    'plant pot','flower pot','planter','planter box','drip tray',
    'garden stake','plant stake','plant support','watering can',
    'spray bottle','garden glove','seed tray','plant label','plant tag',
    'garden tie','plant tie','bird feeder','bird house',
    'craft paper','glue stick','craft glue','ribbon','twine',
    'jute twine','hemp cord','thread','sewing thread','yarn',
    'bead','beads','sequin','glitter','button','buttons',
    'zipper','zippers','elastic','felt','felt sheet','foam sheet',
    'tote bag','canvas tote','shopping bag','reusable bag','mesh bag',
    'laundry bag','storage bag','vacuum bag','zip bag','gift bag',
    'drawstring bag','pouch','cosmetic pouch',
    'microfiber cloth','cleaning cloth','sponge','cleaning sponge',
    'scrub brush','cleaning brush','dustpan','broom','mop','mop head',
    'duster','squeegee','lint roller','spray bottle',
    'cutting board','chopping board','measuring cup','measuring spoon',
    'mixing bowl','serving bowl','food container','lunch box',
    'ice cube tray','silicone mat','baking mat','oven mitt','pot holder',
    'trivet','dish rack','utensil holder','spoon rest','storage jar',
    'water bottle','reusable straw','silicone straw',
    'coaster','placemat','table mat','napkin holder','paper towel holder',
    'soap dish','soap dispenser','toothbrush holder','toilet brush',
    'bath mat','shower mat','shower curtain','towel rail','towel bar',
    'toilet roll holder','bathroom shelf',
    'cushion cover','pillow cover','pillowcase','throw blanket',
    'curtain','curtain rod','curtain ring','rug','floor mat','door mat',
    'table cloth','table runner',
    'luggage tag','packing cube','toiletry bag','passport holder',
    'travel pillow','neck pillow','sleep mask','eye mask','ear plug',
    'country flag','national flag','flag pendant','flag necklace',
    'map pendant','map necklace','country pendant','country necklace',
    'flag keychain','flag keyring','flag pin','flag badge','flag patch',
    'flag bracelet','flag earring','flag ring','flag charm',
    'country map','map charm','map keychain','map keyring',
    'sewing needle','sewing pin','pin cushion','thimble',
    'seam ripper','measuring tape','bobbin',
]
# ============================================================================
# PRECOMPILED HARD BLOCK RULES (pattern, score, rule_name, category)
# ============================================================================
_RAW_HARD_BLOCK_RULES = [
    (r'\b(kni[fv]e|knives|blade|blades|dagger|machete|katana|sword|swords)\b', 100, 'BLADED_ITEM', 'weapons'),
    (r'\b(scissors?|shears|cutter|cutters|razor|scalpel|cleaver)\b', 100, 'BLADED_ITEM', 'weapons'),
    (r'\b(axe|axes|hatchet|bayonet|balisong|karambit|kukri)\b', 100, 'BLADED_ITEM', 'weapons'),
    (r'\b(chainsaw|chain.?saw)\b', 100, 'CHAINSAW', 'weapons'),
    (r'\b(chainsaw.?chain|saw.?chain|chain.?blade|cutting.?chain)\b', 100, 'CHAINSAW_CHAIN', 'weapons'),
    (r'\b(chainsaw.?bar|guide.?bar|saw.?bar)\b', 90, 'CHAINSAW_PART', 'weapons'),
    (r'\b(chain.?sharpener|chain.?file|chainsaw.?file)\b', 80, 'CHAINSAW_ACCESSORY', 'weapons'),
    (r'\b(saw.?blade|circular.?saw|jig.?saw|sabre.?saw|reciprocating.?saw)\b', 90, 'SAW_BLADE', 'weapons'),
    (r'\b(hedge.?trimmer|hedge.?cutter|brush.?cutter|strimmer)\b', 80, 'GARDEN_CUTTER', 'weapons'),
    (r'\b(pruner|pruning|secateurs|loppers|garden.?shears|tree.?saw)\b', 90, 'GARDEN_BLADE', 'weapons'),
    (r'\b(mandoline|mandolin.?slicer|vegetable.?slicer|food.?slicer)\b', 80, 'KITCHEN_BLADE', 'weapons'),
    (r'\b(box.?cutter|utility.?blade|snap.?off.?blade|stanley.?blade|craft.?blade)\b', 90, 'UTILITY_BLADE', 'weapons'),
    (r'\b(wood.?chisel|carving.?chisel|carving.?tool|carving.?set)\b', 80, 'CHISEL', 'weapons'),
    (r'\b(plane.?blade|hand.?plane|spokeshave)\b', 80, 'WOODWORK_BLADE', 'weapons'),
    (r'\b(rotary.?blade|rotary.?cutter|fabric.?cutter|paper.?cutter|paper.?trimmer)\b', 80, 'CRAFT_BLADE', 'weapons'),
    (r'\b(wire.?cutter|cable.?cutter|bolt.?cutter|tin.?snip|pipe.?cutter)\b', 80, 'METAL_CUTTER', 'weapons'),
    (r'\b(nipper|nippers|plier.?cutter|side.?cutter|end.?cutter|diagonal.?cutter)\b', 80, 'CUTTER_TOOL', 'weapons'),
    (r'\b(blade.?replacement|replacement.?blade|spare.?blade|refill.?blade)\b', 85, 'REPLACEMENT_BLADE', 'weapons'),
    (r'\b(sharpener|sharpening|honing|whetstone|strop)\b', 80, 'SHARPENER', 'weapons'),
    (r'\b(peeler|potato.?peeler|vegetable.?peeler|julienne.?peeler)\b', 80, 'PEELER', 'weapons'),
    (r'\b(grater|cheese.?grater|zester|microplane)\b', 70, 'GRATER', 'weapons'),
    (r'\b(scraper|paint.?scraper|wall.?scraper|glass.?scraper|putty.?knife)\b', 80, 'SCRAPER', 'weapons'),
    (r'\b(ice.?scraper)\b', 70, 'SCRAPER', 'weapons'),
    (r'\b(snip|snips|tin.?snips?|aviation.?snips?|garden.?snips?)\b', 80, 'SNIPS', 'weapons'),
    (r'\b(slicer|meat.?slicer|bread.?slicer|egg.?slicer)\b', 80, 'SLICER', 'weapons'),
    (r'\b(switchblade|flick.?knife|gravity.?knife|butterfly.?knife)\b', 100, 'PROHIBITED_WEAPON', 'weapons'),
    (r'\b(titanium.?toothpick|edc.?pick|tactical.?pen|glass.?breaker)\b', 100, 'DISGUISED_BLADE', 'weapons'),
    (r'\b(multitool|multi.?tool|leatherman|victorinox|swiss.?army)\b', 100, 'MULTITOOL', 'weapons'),
    (r'\b(gun|firearm|rifle|pistol|shotgun|ammunition|ammo)\b', 100, 'FIREARM', 'weapons'),
    (r'\b(airsoft|bb.?gun|pellet.?gun|air.?rifle|air.?pistol)\b', 100, 'AIRSOFT', 'weapons'),
    (r'\b(crossbow|compound.?bow|recurve.?bow)\b', 100, 'WEAPON', 'weapons'),
    (r'\b(stun.?gun|taser|pepper.?spray|mace|baton|knuckle.?duster)\b', 100, 'WEAPON', 'weapons'),
    (r'\b(nunchaku|shuriken|throwing.?star|ninja.?star)\b', 100, 'WEAPON', 'weapons'),
    (r'\b(lock.?pick|lock.?picking|bump.?key)\b', 100, 'LOCKPICK', 'weapons'),
    (r'\b(corkscrew|cork.?screw)\b', 90, 'CORKSCREW', 'weapons'),
    (r'\b(can.?opener|tin.?opener|bottle.?opener|wine.?opener|beer.?opener)\b', 90, 'OPENER_BLADE', 'weapons'),
    (r'\b(lid.?remover|lid.?opener|jar.?opener|cap.?opener|cap.?twister)\b', 80, 'OPENER_BLADE', 'weapons'),
    (r'\b(waiter.?friend|sommelier|wing.?corkscrew|lever.?corkscrew)\b', 90, 'CORKSCREW', 'weapons'),
    (r'\b(logo.?projector|ghost.?shadow|puddle.?light|door.?projector)\b', 100, 'LOGO_PROJECTOR', 'trademark'),
    (r'\b(welcome.?light|courtesy.?light|shadow.?light)\b', 100, 'LOGO_PROJECTOR', 'trademark'),
    (r'\b(car.?emblem|trunk.?badge|grille.?emblem|hood.?emblem)\b', 90, 'CAR_BADGE', 'trademark'),
    (r'\b(steering.?wheel.?badge|wheel.?cap.?logo|center.?cap.?logo)\b', 90, 'CAR_BADGE', 'trademark'),
    (r'\b(lego|legoo|l[\.\-_ ]?e[\.\-_ ]?g[\.\-_ ]?o)\b', 100, 'LEGO', 'vero'),
    (r'\b(minifig|minifigure|mini.?figure|brick.?figure)\b', 100, 'LEGO_FIGURE', 'vero'),
    (r'\b(building.?blocks?|brick.?blocks?|compatible.?bricks?)\b', 90, 'BUILDING_BLOCKS', 'vero'),
    (r'\b(lepin|mould.?king|cada|xingbao|sembo|cobi|enlighten|wange|panlos)\b', 100, 'LEGO_CLONE', 'vero'),
    (r'\b(moc|my.?own.?creation)\b', 80, 'MOC', 'vero'),
    (r'\b(vehicle.?robot|robot.?vehicle|car.?robot|robot.?car|tank.?robot|robot.?tank)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot.?toy|mecha.?toy|mecha.?robot|mecha.?model|robot.?model)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot.?figure|mecha.?figure|action.?robot|robot.?action)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot.?building.?block|vehicle.?robot.?building|robot.?brick)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(helicopter.?robot|plane.?robot|aircraft.?robot|jet.?robot|truck.?robot)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(dinosaur.?robot|dragon.?robot|snake.?robot|beast.?robot|animal.?robot)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(combine|combiner|gestalt|merge).*(robot|mecha)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot|mecha).*(combine|combiner|gestalt|merge)\b', 90, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(5.?in.?1|6.?in.?1|3.?in.?1).*(robot|mecha)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot|mecha).*(5.?in.?1|6.?in.?1|3.?in.?1)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(warrior|raider|guardian|defender|commander|striker).*(robot|mech)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(robot|mech).*(warrior|raider|guardian|defender|commander|striker)\b', 85, 'MECHA_KNOCKOFF', 'vero'),
    (r'\b(gel.?cushion|gel.?seat|honeycomb.?cushion|egg.?cushion|egg.?sitter)\b', 100, 'PURPLE_PATENT', 'patent'),
    (r'\b(seat.?gap.?filler|drop.?stop|gap.?filler|gap.?pad)\b', 100, 'DROP_STOP_PATENT', 'patent'),
    (r'\b(replica|counterfeit|knockoff|knock.?off|fake|imitation)\b', 100, 'COUNTERFEIT', 'counterfeit'),
    (r'\b(1\s*:\s*1|aaa.?quality|super.?clone|mirror.?quality)\b', 100, 'COUNTERFEIT', 'counterfeit'),
    (r'\b(inspired|inspired.?by|style.?of|looks?.?like|same.?as)\b', 80, 'COUNTERFEIT_INDICATOR', 'counterfeit'),
    (r'\b(dupe|dupes|homage|tribute)\b', 80, 'COUNTERFEIT_INDICATOR', 'counterfeit'),
    (r'\b(premium.?copy|a.?grade|top.?grade|best.?version)\b', 100, 'COUNTERFEIT', 'counterfeit'),
    (r'\b(original.?quality|same.?material|same.?as.?retail)\b', 90, 'COUNTERFEIT', 'counterfeit'),
    (r'\b(factory.?direct|factory.?version|qc.?pics?|qc.?photo)\b', 80, 'COUNTERFEIT_INDICATOR', 'counterfeit'),
    (r'\b(with.?box|no.?box|comes.?with.?box|full.?set)\b', 50, 'COUNTERFEIT_SIGNAL', 'counterfeit'),
    (r'\b(ua|unauthorized.?authentic|unauth)\b', 90, 'COUNTERFEIT', 'counterfeit'),
    (r'\b(stock\s*x|stockx|grailed|auth.?check)\b', 80, 'COUNTERFEIT_INDICATOR', 'counterfeit'),
    (r'\b(batch|pk.?batch|og.?batch|ljr|pk.?god|h12)\b', 90, 'COUNTERFEIT_BATCH', 'counterfeit'),
    (r'\b(925|sterling.?silver|solid.?silver|real.?silver|s925)\b', 90, 'PRECIOUS_METAL', 'misrepresentation'),
    (r'\b(18k|14k|24k|10k)\s*(gold|white.?gold|rose.?gold|yellow.?gold)(?!\s*color)\b', 90, 'PRECIOUS_METAL', 'misrepresentation'),
    (r'\b(solid.?gold|real.?gold|gold.?vermeil|pure.?gold)\b', 90, 'PRECIOUS_METAL', 'misrepresentation'),
    (r'\b(moissanite|lab.?diamond|simulated.?diamond|cubic.?zirconia)\b', 80, 'DIAMOND_CLAIM', 'misrepresentation'),
    (r'\b(cpap|bipap|apap|nebulizer|nebuliser|oxygen.?concentrator)\b', 100, 'MEDICAL_DEVICE', 'medical'),
    (r'\b(hearing.?aid|pacemaker|defibrillator|insulin.?pump)\b', 100, 'MEDICAL_DEVICE', 'medical'),
    (r'\b(contact.?lens|prescription|surgical.?instrument)\b', 90, 'MEDICAL', 'medical'),
    (r'\b(fda.?approved|clinically.?proven|medically.?proven)\b', 100, 'MEDICAL_CLAIM', 'medical'),
    (r'\b(cures?|treats?\b.*\bdisease|heals?|prevents?\s+disease)\b', 100, 'MEDICAL_CLAIM', 'medical'),
    (r'\b(dpf.?delete|egr.?delete|adblue.?delete|cat.?delete|decat|de.?cat)\b', 100, 'EMISSIONS_DEFEAT', 'illegal'),
    (r'\b(ecu.?remap|chip.?tun|stage.?[123])\b', 90, 'EMISSIONS_DEFEAT', 'illegal'),
    (r'\b(mileage.?correction|odometer.?correction|mileage.?stopper|can.?filter)\b', 100, 'EMISSIONS_DEFEAT', 'illegal'),
    (r'\b(spy.?cam|hidden.?camera|pinhole.?camera|nanny.?cam|covert.?camera)\b', 100, 'SURVEILLANCE', 'surveillance'),
    (r'\b(gps.?tracker|tracking.?device|car.?tracker|bug.?detector)\b', 100, 'SURVEILLANCE', 'surveillance'),
    (r'\b(signal.?jammer|phone.?jammer|wifi.?jammer|gps.?jammer)\b', 100, 'JAMMER', 'illegal'),
    (r'\b(huawei|zte|hikvision|dahua|hytera)\b', 100, 'FCC_BANNED', 'compliance'),
    (r'(www\.|\.com\b|\.co\.uk\b|\.net\b|\.org\b|https?://)', 100, 'LINK_VIOLATION', 'links'),
    (r'[\w\.-]+@[\w\.-]+\.\w{2,}', 100, 'EMAIL_DETECTED', 'links'),
    (r'\b(contact.?us|call.?us|email.?us|whatsapp|telegram|wechat)\b', 100, 'CONTACT_VIOLATION', 'links'),
    (r'\b(visit.?our|check.?our|see.?our|find.?us|our.?website)\b', 80, 'OFF_PLATFORM', 'links'),
    (r'\b(ce.?marked|ukca.?marked|ce.?certified|ukca.?certified)\b', 60, 'SAFETY_CLAIM', 'safety'),
    (r'\b(explosive|flammable|corrosive|radioactive|toxic)\b', 100, 'HAZMAT', 'hazardous'),
    (r'\b(firework|firecracker|gunpowder|poison)\b', 100, 'HAZMAT', 'hazardous'),
    (r'\b(vape|vaping|e.?cigarette|e.?cig|e.?liquid|vape.?pen|vape.?mod)\b', 100, 'VAPE', 'restricted'),
    (r'\b(bong|grinder|rolling.?paper|pipe.?screen|dab.?rig|weed|cannabis|thc|cbd)\b', 100, 'DRUG_PARA', 'restricted'),
    (r'\b(laser.?pointer|laser.?pen|burning.?laser|high.?power.?laser)\b', 100, 'LASER', 'restricted'),
    (r'\b(weight.?loss|fat.?burner|diet.?pill|slimming.?pill|appetite.?suppress)\b', 100, 'SUPPLEMENT', 'restricted'),
    (r'\b(steroid|anabolic|sarm|sarms|prohormone|testosterone.?boost)\b', 100, 'SUPPLEMENT', 'restricted'),
    (r'\b(kratom|dnp|hgh|growth.?hormone)\b', 100, 'SUPPLEMENT', 'restricted'),
    (r'\b(fake.?id|counterfeit.?money|prop.?money|fake.?money)\b', 100, 'GOVT_RESTRICTED', 'illegal'),
    (r'\b(police.?badge|police.?uniform|military.?uniform)\b', 80, 'GOVT_RESTRICTED', 'restricted'),
    (r'\b(product.?recall|recalled.?item|safety.?recall|cpsc.?recall)\b', 100, 'PRODUCT_RECALL', 'product_safety'),
    (r'\b(no.?plug|eu.?plug|us.?plug|2.?pin.?plug)\b.*\b(uk|britain|british)\b', 80, 'WRONG_PLUG', 'product_safety'),
    (r'\b(uk|britain|british)\b.*\b(no.?plug|eu.?plug|us.?plug|2.?pin.?plug)\b', 80, 'WRONG_PLUG', 'product_safety'),
    (r'\b(neodymium|rare.?earth)\s*(magnet|ball|sphere|cube)\b.*\b(small|tiny|mini|5mm|3mm|set)\b', 90, 'MAGNET_SAFETY', 'product_safety'),
    (r'\b(small|tiny|mini|5mm|3mm|set)\b.*\b(neodymium|rare.?earth)\s*(magnet|ball|sphere|cube)\b', 90, 'MAGNET_SAFETY', 'product_safety'),
    (r'\b(magnetic.?ball|buckyballs?|magnet.?ball|magnet.?sphere|zen.?magnets?)\b', 90, 'MAGNET_SAFETY', 'product_safety'),
    (r'\b(baby.?walker)\b', 80, 'CHILD_SAFETY', 'product_safety'),
    (r'\b(button.?battery|coin.?cell)\b.*\b(toy|child|kid|baby|infant)\b', 85, 'BUTTON_BATTERY_TOY', 'product_safety'),
    (r'\b(class\s*[34]|class\s*iii|class\s*iv)\s*(laser)\b', 100, 'HIGH_POWER_LASER', 'product_safety'),
    (r'\b(bs\s*1363|bs\s*546|bs\s*en)\b.*\b(approved|certified|compliant)\b', 70, 'ELECTRICAL_SAFETY_CLAIM', 'product_safety'),
    (r'\b(lambda.?eliminator|o2.?eliminator|o2.?spacer|oxygen.?sensor.?bypass)\b', 90, 'EMISSIONS_BYPASS', 'emissions'),
    (r'\b(catalytic.?converter.?delete|cat.?bypass|test.?pipe|straight.?pipe.?exhaust)\b', 90, 'EMISSIONS_BYPASS', 'emissions'),
    (r'\b(exhaust.?flame|flame.?kit|exhaust.?fire|backfire.?kit)\b', 80, 'EXHAUST_MOD', 'emissions'),
    (r'\b(rolling.?coal|smoke.?tune|black.?smoke.?tune)\b', 100, 'EMISSIONS_DEFEAT', 'emissions'),
    (r'\b(nazi|swastika|ss.?uniform|ss.?dagger|third.?reich|iron.?cross)\b', 100, 'NAZI_MEMORABILIA', 'offensive'),
    (r'\b(kkk|ku.?klux|white.?power|white.?supremac|aryan|1488|14.?words)\b', 100, 'HATE_SYMBOL', 'offensive'),
    (r'\b(confederate.?flag|rebel.?flag|dixie.?flag)\b', 90, 'OFFENSIVE_FLAG', 'offensive'),
    (r'\b(golliwog|gollywog|robertson.?jam)\b', 100, 'OFFENSIVE_ITEM', 'offensive'),
    (r'\b(real.?ivory|genuine.?ivory|elephant.?ivory|ivory.?tusk|ivory.?carving)\b', 100, 'IVORY', 'animal_products'),
    (r'\b(real.?fur|genuine.?fur|mink.?fur|fox.?fur|rabbit.?fur|chinchilla.?fur|sable.?fur)\b', 90, 'REAL_FUR', 'animal_products'),
    (r'\b(tortoiseshell|hawksbill|sea.?turtle|coral.?jewelry|coral.?jewellery|shark.?fin)\b', 90, 'CITES', 'animal_products'),
    (r'\b(taxiderm|mounted.?head|trophy.?mount|animal.?skull)\b', 80, 'TAXIDERMY', 'animal_products'),
    (r'\b(rhino.?horn|tiger.?bone|bear.?bile|pangolin)\b', 100, 'ENDANGERED', 'animal_products'),
    (r'\b(sex.?toy|vibrator|dildo|flesh.?light|fleshlight|butt.?plug|cock.?ring)\b', 100, 'ADULT_ITEM', 'adult'),
    (r'\b(bondage|bdsm|fetish.?wear|gimp|ball.?gag|handcuffs?.?leather)\b', 100, 'ADULT_ITEM', 'adult'),
    (r'\b(tobacco|cigarette|cigar|rolling.?tobacco|pipe.?tobacco)\b', 90, 'TOBACCO', 'restricted'),
    (r'\b(alcohol|spirits?|whisky|whiskey|vodka|wine|beer|gin)\b.*\b(bottle|gift|set)\b', 70, 'ALCOHOL', 'restricted'),
    (r'\b(catalytic.?converter|cat.?converter)\b', 90, 'CATALYTIC_CONVERTER', 'restricted'),
    (r'\b(number.?plate.?cover|plate.?flipper|plate.?hide|plate.?obscure|stealth.?plate)\b', 100, 'PLATE_HIDER', 'illegal'),
    (r'\b(radar.?detector|speed.?camera.?detector|laser.?jammer|lidar.?jammer)\b', 100, 'RADAR_DETECTOR', 'illegal'),
    (r'\b(tint.?spray|window.?tint.?spray|spray.?on.?tint)\b', 70, 'WINDOW_TINT', 'restricted'),
    (r'\b(seatbelt.?alarm.?stopper|seat.?belt.?cancel|seatbelt.?bypass|buckle.?alarm.?stop)\b', 100, 'SEATBELT_DEFEAT', 'product_safety'),
    (r'\b(airbag.?cancel|airbag.?bypass|airbag.?resistor|srs.?bypass)\b', 100, 'AIRBAG_DEFEAT', 'product_safety'),
    (r'\b(modchip|mod.?chip|jailbreak|r4.?card|flash.?cart|sky3ds)\b', 100, 'CIRCUMVENTION', 'illegal'),
    (r'\b(iptv|iptv.?box|iptv.?subscription|iptv.?12.?month)\b', 100, 'IPTV', 'illegal'),
    (r'\b(card.?sharing|cccam|oscam|cs.?server|cs.?line)\b', 100, 'CARD_SHARING', 'illegal'),
    (r'\b(cracked|keygen|serial.?key|license.?key|activation.?key|product.?key)\b', 90, 'PIRACY', 'illegal'),
    (r'\b(kodi.?box|android.?box.?loaded|fully.?loaded|jailbroken)\b', 90, 'LOADED_BOX', 'illegal'),
    (r'\b(pesticide|insecticide|herbicide|fungicide|rodenticide|rat.?poison)\b', 90, 'PESTICIDE', 'restricted'),
    (r'\b(cyanide|arsenic|mercury.?liquid|asbestos)\b', 100, 'HAZARDOUS_CHEMICAL', 'hazardous'),
    (r'\b(chloroform|ghb|rohypnol|date.?rape)\b', 100, 'CONTROLLED_SUBSTANCE', 'illegal'),
    (r'\b(miracle.?cure|cancer.?cure|covid.?cure|guaranteed.?cure)\b', 100, 'HEALTH_CLAIM', 'medical'),
    (r'\b(colloidal.?silver|mms|miracle.?mineral|chlorine.?dioxide)\b', 100, 'QUACK_REMEDY', 'medical'),
    (r'\b(raw.?milk|unpasteurised.?milk|unpasteurized.?milk)\b', 90, 'FOOD_SAFETY', 'restricted'),
    (r'\b(concert.?ticket|event.?ticket|festival.?ticket|gig.?ticket|match.?ticket)\b', 80, 'EVENT_TICKET', 'restricted'),
    (r'\b(ticket.?resale|spare.?ticket|extra.?ticket)\b', 70, 'EVENT_TICKET', 'restricted'),
    (r'\b(bitcoin.?miner|crypto.?miner|asic.?miner|antminer|mining.?rig)\b', 80, 'CRYPTO_MINER', 'restricted'),
    (r'\b(gift.?card|itunes.?card|google.?play.?card|steam.?card|psn.?card|xbox.?card)\b', 80, 'GIFT_CARD', 'restricted'),
    (r'\b(ship|shipped|shipping|dispatch|dispatched|send|sent)\b.*\b(from|via|direct)\b.*\b(china|chinese|hong.?kong|hk|shenzhen|guangzhou|yiwu|aliexpress|alibaba|overseas|abroad|asia|warehouse)\b', 100, 'ORIGIN_DISCLOSURE', 'dropship_policy'),
    (r'\b(from|via|direct)\b.*\b(china|chinese|hong.?kong|hk|shenzhen|guangzhou|yiwu|aliexpress|alibaba|overseas|abroad)\b.*\b(ship|shipping|dispatch|delivery|warehouse)\b', 100, 'ORIGIN_DISCLOSURE', 'dropship_policy'),
    (r'\b(chinese.?warehouse|china.?warehouse|overseas.?warehouse|hong.?kong.?warehouse)\b', 100, 'ORIGIN_DISCLOSURE', 'dropship_policy'),
    (r'\b(drop.?ship|dropship|drop.?shipping|dropshipping)\b', 100, 'DROPSHIP_MENTION', 'dropship_policy'),
    (r'\b(aliexpress|alibaba|wish\.com|temu|dhgate|banggood|gearbest|1688)\b', 100, 'MARKETPLACE_MENTION', 'dropship_policy'),
    (r'\b(direct.?from.?manufacturer|factory.?direct.?ship|ship.?direct.?from)\b', 90, 'ORIGIN_DISCLOSURE', 'dropship_policy'),
    (r'\b(import|imported)\s+(from|direct)\s+(china|asia|overseas)\b', 90, 'ORIGIN_DISCLOSURE', 'dropship_policy'),
    (r'\b(7.?30|10.?25|15.?30|20.?40)\s*(business)?\s*days?\s*(delivery|shipping)\b', 70, 'LONG_DELIVERY', 'dropship_policy'),
    (r'\b(self.?defen[cs]e|self.?protection|personal.?protection|personal.?safety)\b.*\b(weapon|tool|device|spray|alarm|keychain|ring|pen|stick|baton|whip)\b', 100, 'SELF_DEFENCE_WEAPON', 'illegal'),
    (r'\b(weapon|tool|device|spray|keychain|ring|pen|stick|baton)\b.*\b(self.?defen[cs]e|self.?protection|personal.?protection)\b', 100, 'SELF_DEFENCE_WEAPON', 'illegal'),
    (r'\b(self.?defen[cs]e|personal.?alarm|attack.?alarm|rape.?alarm)\b', 80, 'SELF_DEFENCE', 'restricted'),
    (r'\b(kubotan|kubaton|tactical.?keychain|tactical.?ring|tactical.?whip)\b', 100, 'SELF_DEFENCE_WEAPON', 'illegal'),
    (r'\b(monkey.?fist|slungshot|slingshot.?weapon|cat.?ears?.?keychain)\b', 100, 'DISGUISED_WEAPON', 'illegal'),
    (r'\b(body.?armou?r|stab.?vest|stab.?proof|bullet.?proof|ballistic.?vest)\b', 100, 'BODY_ARMOUR', 'restricted'),
    (r'\b(kevlar.?vest|plate.?carrier|armou?r.?plate|ballistic.?plate|nij.?level)\b', 100, 'BODY_ARMOUR', 'restricted'),
    (r'\b(anti.?stab|slash.?proof|slash.?resistant.?vest|cut.?resistant.?vest)\b', 90, 'BODY_ARMOUR', 'restricted'),
    (r'\b(signal.?booster|signal.?repeater|signal.?amplifier|cell.?booster)\b', 90, 'SIGNAL_BOOSTER', 'restricted'),
    (r'\b(mobile.?booster|mobile.?repeater|4g.?booster|5g.?booster|gsm.?booster)\b', 90, 'SIGNAL_BOOSTER', 'restricted'),
    (r'\b(network.?booster|network.?repeater|network.?extender|femtocell)\b', 80, 'SIGNAL_BOOSTER', 'restricted'),
    (r'\b(hoverboard|hover.?board|self.?balancing.?scooter|balance.?board)\b', 90, 'HOVERBOARD', 'product_safety'),
    (r'\b(electric.?scooter|e.?scooter|e.?skateboard|electric.?skateboard)\b', 80, 'E_SCOOTER', 'restricted'),
    (r'\b(electric.?bike.?conversion|ebike.?kit|e.?bike.?kit)\b', 70, 'EBIKE_KIT', 'restricted'),
    (r'\b(usb.?killer|usb.?kill|emp.?device|emp.?generator|emp.?jammer)\b', 100, 'MALICIOUS_DEVICE', 'illegal'),
    (r'\b(deauth|deauther|wifi.?deauth|wifi.?pineapple|flipper.?zero)\b', 90, 'HACKING_DEVICE', 'illegal'),
    (r'\b(card.?skimmer|skimming.?device|rfid.?clon|rfid.?copy|nfc.?clon|nfc.?copy)\b', 100, 'FRAUD_DEVICE', 'illegal'),
    (r'\b(rfid|nfc)\b.*\b(clone|copy|writ|duplicat)\b', 100, 'FRAUD_DEVICE', 'illegal'),
    (r'\b(clone|copy|writ|duplicat)\b.*\b(rfid|nfc)\b', 100, 'FRAUD_DEVICE', 'illegal'),
    (r'\b(imei.?unlock|imei.?repair|imei.?change|imei.?clean)\b', 100, 'IMEI_SERVICE', 'illegal'),
    (r'\b(icloud.?unlock|icloud.?bypass|icloud.?removal|frp.?bypass|frp.?unlock)\b', 100, 'PHONE_UNLOCK', 'illegal'),
    (r'\b(phone.?unlock.?service|network.?unlock.?code|unlock.?code)\b', 80, 'PHONE_UNLOCK', 'restricted'),
    (r'\b(sim.?clone|sim.?copy|sim.?reader|sim.?writer)\b', 100, 'SIM_CLONE', 'illegal'),
    (r'\b(skin.?whitening|skin.?bleaching|skin.?lightening|skin.?brightening)\b.*\b(cream|lotion|serum|gel|soap|pill|injection)\b', 90, 'SKIN_WHITENING', 'restricted'),
    (r'\b(cream|lotion|serum|gel|soap|pill|injection)\b.*\b(skin.?whitening|skin.?bleaching|skin.?lightening)\b', 90, 'SKIN_WHITENING', 'restricted'),
    (r'\b(glutathione.?inject|glutathione.?iv|hydroquinone|mercury.?cream)\b', 100, 'SKIN_WHITENING_BANNED', 'illegal'),
    (r'\b(kojic.?acid|arbutin)\b.*\b(strong|extra|maximum|professional)\b', 70, 'SKIN_WHITENING', 'restricted'),
    (r'\b(slot.?machine|fruit.?machine|poker.?machine|gambling.?machine)\b', 80, 'GAMBLING', 'restricted'),
    (r'\b(roulette.?wheel|card.?counting|marked.?cards|loaded.?dice|cheat.?dice)\b', 90, 'GAMBLING_CHEAT', 'restricted'),
    (r'\b(poker.?chip.?set|casino.?chip).*\b(professional|real|genuine)\b', 60, 'GAMBLING', 'restricted'),
    (r'\b(human.?remains|human.?bone|human.?skull|human.?skeleton|human.?teeth)\b', 100, 'HUMAN_REMAINS', 'prohibited'),
    (r'\b(human.?hair.?extension|human.?blood|body.?fluid|urine.?sample)\b', 90, 'BODY_FLUIDS', 'prohibited'),
    (r'\b(used.?underwear|worn.?underwear|used.?panties|worn.?panties|used.?socks)\b', 100, 'USED_INTIMATE', 'prohibited'),
    (r'\b(breast.?milk|placenta|umbilical)\b', 80, 'BODY_PRODUCT', 'prohibited'),
    (r'\b(car.?key.?program|key.?program|key.?clone|key.?copy|key.?cutting)\b', 80, 'KEY_PROGRAMMING', 'restricted'),
    (r'\b(transponder.?key.?program|immobilis?er.?bypass|immobilis?er.?emulator)\b', 100, 'IMMOBILISER_BYPASS', 'illegal'),
    (r'\b(obd.?key.?program|eeprom.?key|key.?emulator)\b', 90, 'KEY_PROGRAMMING', 'restricted'),
    (r'\b(relay.?attack|keyless.?entry.?hack|keyless.?theft|signal.?relay)\b', 100, 'THEFT_DEVICE', 'illegal'),
    (r'\b(number.?plate.?maker|number.?plate.?printer|reg.?plate.?maker)\b', 100, 'PLATE_MAKER', 'restricted'),
    (r'\b(show.?plate|display.?plate|novelty.?plate|custom.?number.?plate)\b', 80, 'SHOW_PLATE', 'restricted'),
    (r'\b(3d.?gel.?plate|4d.?plate|4d.?number.?plate|3d.?number.?plate)\b', 80, 'CUSTOM_PLATE', 'restricted'),
    (r'\b(nitrous.?oxide|laughing.?gas|nos.?canister|nos.?cracker|nos.?charger)\b', 100, 'NITROUS', 'illegal'),
    (r'\b(whippits?|whippets?|nang|nangs|cream.?charger|n2o.?charger)\b', 90, 'NITROUS', 'illegal'),
    (r'\b(cream.?dispenser|whipped.?cream.?charger)\b.*\b(bulk|100|200|500|party)\b', 80, 'NITROUS_SIGNAL', 'restricted'),
    (r'\b(live.?animal|live.?insect|live.?spider|live.?reptile|live.?fish|live.?bird)\b', 100, 'LIVE_ANIMAL', 'prohibited'),
    (r'\b(live.?plant|live.?tree|live.?shrub|live.?cutting|rooted.?cutting)\b', 80, 'LIVE_PLANT', 'restricted'),
    (r'\b(seed|seeds)\b.*\b(cannabis|marijuana|coca|opium|poppy|psilocybin|magic.?mushroom)\b', 100, 'DRUG_SEEDS', 'illegal'),
    (r'\b(cannabis|marijuana|coca|opium|psilocybin|magic.?mushroom)\b.*\b(seed|seeds)\b', 100, 'DRUG_SEEDS', 'illegal'),
    (r'\b(spore.?syringe|spore.?print|mushroom.?grow.?kit)\b', 90, 'DRUG_GROW', 'restricted'),
    (r'\b(grow.?tent|grow.?light|hydroponic)\b.*\b(cannabis|marijuana|weed)\b', 100, 'DRUG_GROW', 'illegal'),
    (r'\b(skip.?trac|people.?finder|people.?search|background.?check)\b', 70, 'SKIP_TRACING', 'restricted'),
    (r'\b(spy.?software|stalker.?ware|spy.?app|phone.?spy|phone.?monitor)\b', 100, 'SPYWARE', 'illegal'),
    (r'\b(voice.?recorder|audio.?recorder)\b.*\b(hidden|covert|spy|secret|disguised)\b', 100, 'COVERT_RECORDING', 'surveillance'),
    (r'\b(hidden|covert|spy|secret|disguised)\b.*\b(voice.?recorder|audio.?recorder)\b', 100, 'COVERT_RECORDING', 'surveillance'),
    (r'\b(counterfeit.?detect|fake.?note.?detect|uv.?money.?check|bill.?counter)\b', 60, 'COUNTERFEIT_DETECTOR', 'restricted'),
    (r'\b(money.?print|currency.?print|bank.?note.?template|bank.?note.?paper)\b', 100, 'COUNTERFEIT_TOOL', 'illegal'),
    (r'\b(nhs.?uniform|nhs.?badge|nhs.?id|paramedic.?uniform|ambulance.?uniform)\b', 90, 'GOVT_UNIFORM', 'restricted'),
    (r'\b(hi.?vis|high.?vis)\b.*\b(police|ambulance|paramedic|fire)\b', 80, 'EMERGENCY_UNIFORM', 'restricted'),
    (r'\b(police|ambulance|paramedic|fire)\b.*\b(hi.?vis|high.?vis)\b', 80, 'EMERGENCY_UNIFORM', 'restricted'),
    (r'\b(blue.?light|siren|strobe)\b.*\b(emergency|police|ambulance|fire)\b', 90, 'EMERGENCY_EQUIP', 'restricted'),
    (r'\b(emergency|police|ambulance|fire)\b.*\b(blue.?light|siren|strobe)\b', 90, 'EMERGENCY_EQUIP', 'restricted'),
    (r'\b(eu.?plug|european.?plug|2.?pin|shuko|schuko|type.?[cf].?plug)\b', 70, 'NON_UK_PLUG', 'product_safety'),
    (r'\b(us.?plug|american.?plug|type.?[ab].?plug|flat.?pin.?plug)\b', 70, 'NON_UK_PLUG', 'product_safety'),
    (r'\b(adapter.?not.?included|plug.?not.?included|requires.?adapter)\b', 80, 'NO_UK_PLUG', 'product_safety'),
    (r'\b(sunbed|tanning.?bed|tanning.?lamp|uv.?tanning|tanning.?tube)\b', 80, 'TANNING', 'restricted'),
    (r'\b(uv.?nail.?lamp|uv.?gel.?lamp|uv.?led.?lamp)\b.*\b(\d{3,}w|\d{3,}.?watt|professional|salon)\b', 60, 'UV_LAMP', 'restricted'),
    (r'\b(payday.?loan|quick.?loan|instant.?loan|cash.?advance|loan.?shark)\b', 100, 'FINANCIAL_SERVICE', 'prohibited'),
    (r'\b(debt.?consolidat|credit.?repair|credit.?fix|credit.?score.?boost)\b', 80, 'FINANCIAL_SERVICE', 'restricted'),
    (r'\b(nicotine.?pouch|nicotine.?patch|nicotine.?gum|nicotine.?lozenge)\b', 80, 'NICOTINE', 'restricted'),
    (r'\b(hookah|shisha|waterpipe|water.?pipe)\b', 80, 'TOBACCO_ACCESSORY', 'restricted'),
    (r'\b(cigar.?cutter|cigar.?humidor|tobacco.?pipe|smoking.?pipe)\b', 70, 'TOBACCO_ACCESSORY', 'restricted'),
    (r'\b(jacket|jackets|coat|coats|blazer|blazers|parka|parkas|anorak|windbreaker)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(cardigan|cardigans|jumper|jumpers|sweater|sweaters|pullover|pullovers)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(hoodie|hoodies|sweatshirt|sweatshirts|fleece.?top|zip.?up)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(shirt|shirts|blouse|blouses|polo.?shirt|t.?shirt|tee.?shirt|tshirt)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(dress|dresses|skirt|skirts|gown|gowns|romper|rompers|jumpsuit|jumpsuits)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(trousers|pants|jeans|chinos|leggings|joggers|tracksuit|sweatpants)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(shorts|bermuda|swim.?shorts|board.?shorts|gym.?shorts|running.?shorts)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(vest|waistcoat|gilet|bodywarmer|body.?warmer)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(suit|suits|tuxedo|dinner.?jacket|morning.?suit)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(underwear|boxers|briefs|bra|bras|lingerie|nightwear|pyjamas|pajamas)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(swimsuit|swimwear|bikini|tankini|swimming.?costume|bathing.?suit)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(trainers|sneakers|boots|shoes|sandals|slippers|heels|loafers|pumps)\b', 80, 'FOOTWEAR_SIZED', 'returns_risk'),
    (r'\b(football.?boot|running.?shoe|hiking.?boot|work.?boot|safety.?boot|wellington)\b', 80, 'FOOTWEAR_SIZED', 'returns_risk'),
    (r'\b(size\s*[xsmlXSML]{1,3}|size\s*\d{1,2}|uk\s*\d{1,2}|eu\s*\d{2}|us\s*\d{1,2})\b', 70, 'SIZE_INDICATOR', 'returns_risk'),
    (r'\b(small|medium|large|x.?large|xx.?large|xxx.?large|plus.?size|petite|tall)\b.*\b(men|women|ladies|mens|womens|unisex|boys?|girls?)\b', 75, 'GENDERED_SIZING', 'returns_risk'),
    (r'\b(men|women|ladies|mens|womens|unisex|boys?|girls?)\b.*\b(small|medium|large|x.?large|xx.?large|xxx.?large|plus.?size)\b', 75, 'GENDERED_SIZING', 'returns_risk'),
    (r'\b(onesie|dungarees|overalls|playsuit|salopettes|ski.?suit)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(thermal|base.?layer|compression)\b.*\b(top|bottom|shirt|legging|tight)\b', 75, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(top|bottom|shirt|legging|tight)\b.*\b(thermal|base.?layer|compression)\b', 75, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(crop.?top|tank.?top|camisole|tunic|kaftan|kimono.?robe)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(wetsuit|drysuit|rash.?guard|rash.?vest|surf.?suit)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(cycling.?jersey|cycling.?shorts|cycling.?bib|running.?vest)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(costume|fancy.?dress|halloween.?costume|cosplay.?outfit)\b', 75, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(custom|customise[d]?|customize[d]?|customisable|customizable)\b', 80, 'CUSTOM_PRODUCT', 'fulfillment_risk'),
    (r'\b(personali[sz]e[d]?|personali[sz]able|personalisation|personalization)\b', 80, 'CUSTOM_PRODUCT', 'fulfillment_risk'),
    (r'\b(made.?to.?order|bespoke|tailor.?made|build.?your.?own)\b', 80, 'CUSTOM_PRODUCT', 'fulfillment_risk'),
    (r'\b(engrav|monogram|your.?name|your.?text|your.?photo|your.?image|your.?logo)\b', 80, 'CUSTOM_PRODUCT', 'fulfillment_risk'),
    (r'\b(work.?wear|hi.?vis.?vest|hi.?vis.?jacket|overalls?|coveralls?|boiler.?suit)\b', 80, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(gloves?|mittens?|gauntlets?)\b.*\b(size|small|medium|large|[smlx]{1,3})\b', 75, 'CLOTHING_SIZED', 'returns_risk'),
    (r'\b(size|small|medium|large|[smlx]{1,3})\b.*\b(gloves?|mittens?|gauntlets?)\b', 75, 'CLOTHING_SIZED', 'returns_risk'),
]
# ============================================================================
# PRECOMPILED BRAND RULES
# ============================================================================
_RAW_BRAND_RULES = [
    (r'\b(apple|iphone|ipad|macbook|airpods?|airtag|magsafe|ipod|homepod)\b', 100, 'APPLE', 'vero'),
    (r'\b(samsung|galaxy)\b', 90, 'SAMSUNG', 'vero'),
    (r'\b(sony|playstation|ps[2345]|dualsense|dualshock|psvr|psp)\b', 90, 'SONY', 'vero'),
    (r'\b(nintendo|switch|gameboy|game.?boy|wii|3ds|amiibo)\b', 100, 'NINTENDO', 'vero'),
    (r'\b(mario|luigi|zelda|pokemon|pikachu|kirby|donkey.?kong|metroid|splatoon)\b', 100, 'NINTENDO_IP', 'vero'),
    (r'\b(microsoft|xbox|surface|windows)\b', 90, 'MICROSOFT', 'vero'),
    (r'\b(google|pixel|chromecast|nest)\b', 80, 'GOOGLE', 'vero'),
    (r'\b(dji|mavic|phantom|osmo|ronin)\b', 90, 'DJI', 'vero'),
    (r'\b(gopro|go.?pro|hero.?\d+)\b', 90, 'GOPRO', 'vero'),
    (r'\b(bose|quietcomfort|soundlink|soundsport)\b', 90, 'BOSE', 'vero'),
    (r'\b(dyson|airwrap|supersonic|corrale|v\d+.?vacuum)\b', 90, 'DYSON', 'vero'),
    (r'\b(roomba|irobot|braava)\b', 80, 'IROBOT', 'vero'),
    (r'\b(sonos|harman.?kardon|bang.?olufsen|b&o|beoplay)\b', 80, 'AUDIO_BRAND', 'vero'),
    (r'\b(jbl|marshall|sennheiser|jabra)\b', 80, 'AUDIO_BRAND', 'vero'),
    (r'\b(canon|nikon|fujifilm|fuji|lumix|panasonic)\b', 80, 'CAMERA_BRAND', 'vero'),
    (r'\b(xiaomi|redmi|poco|mi.?home|oneplus|oppo|vivo|realme|motorola)\b', 80, 'PHONE_BRAND', 'vero'),
    (r'\b(kitchenaid|kitchen.?aid|vitamix|cuisinart|nespresso|keurig)\b', 80, 'APPLIANCE_BRAND', 'vero'),
    (r'\b(ninja.?blender|ninja.?foodi|ninja.?air|instant.?pot|instapot)\b', 80, 'APPLIANCE_BRAND', 'vero'),
    (r'\b(ring.?doorbell|ring.?camera|simplisafe|arlo|eufy|blink.?camera)\b', 80, 'SMART_HOME', 'vero'),
    (r'\b(alexa|echo.?dot|fire.?stick|fire.?tv|kindle)\b', 80, 'AMAZON_DEVICE', 'vero'),
    (r'\b(oculus|meta.?quest|quest.?2|quest.?3)\b', 80, 'VR_BRAND', 'vero'),
    (r'\b(tesla|cybertruck)\b', 80, 'TESLA', 'vero'),
    (r'\b(ghd|babyliss|chi.?flat|chi.?straightener)\b', 80, 'HAIR_BRAND', 'vero'),
    (r'\b(nike|swoosh|air.?max|air.?jordan|air.?force|just.?do.?it|jumpman)\b', 100, 'NIKE', 'vero'),
    (r'\b(adidas|yeezy|ultraboost|three.?stripes?|trefoil|samba|gazelle)\b', 100, 'ADIDAS', 'vero'),
    (r'\b(gucci|gg.?logo|dionysus|marmont|ophidia)\b', 100, 'GUCCI', 'vero'),
    (r'\b(louis.?vuitton|vuitton|lv.?bag|neverfull|damier|monogram)\b', 100, 'LV', 'vero'),
    (r'\b(chanel|coco.?chanel|double.?c|chanel.?flap|chanel.?boy)\b', 100, 'CHANEL', 'vero'),
    (r'\b(hermes|hermès|birkin|kelly.?bag|constance|evelyne)\b', 100, 'HERMES', 'vero'),
    (r'\b(prada|miu.?miu|prada.?nylon|prada.?re)\b', 100, 'PRADA', 'vero'),
    (r'\b(dior|christian.?dior|lady.?dior|saddle.?bag|dior.?oblique)\b', 100, 'DIOR', 'vero'),
    (r'\b(burberry|nova.?check|tb.?monogram)\b', 100, 'BURBERRY', 'vero'),
    (r'\b(fendi|ff.?logo|baguette.?bag|peekaboo)\b', 100, 'FENDI', 'vero'),
    (r'\b(balenciaga|triple.?s|le.?cagole|hourglass.?bag)\b', 100, 'BALENCIAGA', 'vero'),
    (r'\b(bottega|intrecciato|cassette.?bag|jodie.?bag)\b', 90, 'BOTTEGA', 'vero'),
    (r'\b(ysl|saint.?laurent|loulou|kate.?bag)\b', 100, 'YSL', 'vero'),
    (r'\b(versace|medusa)\b', 100, 'VERSACE', 'vero'),
    (r'\b(valentino|rockstud|vltn)\b', 90, 'VALENTINO', 'vero'),
    (r'\b(givenchy|antigona)\b', 90, 'GIVENCHY', 'vero'),
    (r'\b(dolce.?gabbana|d&g|sicily.?bag)\b', 90, 'DG', 'vero'),
    (r'\b(celine|céline|triomphe|luggage.?bag)\b', 90, 'CELINE', 'vero'),
    (r'\b(loewe|puzzle.?bag|gate.?bag|anagram)\b', 90, 'LOEWE', 'vero'),
    (r'\b(alexander.?mcqueen|mcqueen|skull.?ring)\b', 90, 'MCQUEEN', 'vero'),
    (r'\b(off.?white|virgil.?abloh|industrial.?belt)\b', 100, 'OFFWHITE', 'vero'),
    (r'\b(supreme|box.?logo|bogo)\b', 100, 'SUPREME', 'vero'),
    (r'\b(palm.?angels?|amiri|chrome.?hearts?|goyard|goyardine)\b', 90, 'STREETWEAR', 'vero'),
    (r'\b(bvlgari|bulgari|serpenti|ferragamo|gancini)\b', 90, 'LUXURY', 'vero'),
    (r'\b(tory.?burch|kate.?spade|michael.?kors|mk.?bag|coach.?bag|longchamp|mulberry)\b', 80, 'ACCESSIBLE_LUXURY', 'vero'),
    (r'\b(north.?face|tnf|nuptse|patagonia|arc.?teryx|arcteryx)\b', 90, 'OUTDOOR_BRAND', 'vero'),
    (r'\b(canada.?goose|moncler|stone.?island|cp.?company)\b', 90, 'PREMIUM_OUTERWEAR', 'vero'),
    (r'\b(fear.?of.?god|essentials?.?hoodie|fog.?essentials?)\b', 80, 'STREETWEAR', 'vero'),
    (r'\b(bape|bathing.?ape|stussy|stüssy|palace.?skate|kith)\b', 80, 'STREETWEAR', 'vero'),
    (r'\b(travis.?scott|cactus.?jack|vlone|antisocial.?social)\b', 80, 'STREETWEAR', 'vero'),
    (r'\b(puma|new.?balance|reebok|under.?armour?|asics|onitsuka)\b', 80, 'SPORTSWEAR', 'vero'),
    (r'\b(converse|chuck.?taylor|all.?star|vans|old.?skool)\b', 80, 'SPORTSWEAR', 'vero'),
    (r'\b(carhartt|napapijri|human.?made)\b', 80, 'STREETWEAR', 'vero'),
    (r'\b(crocs|jibbitz)\b', 80, 'CROCS', 'vero'),
    (r'\b(rolex|submariner|daytona|datejust|gmt.?master|oyster.?perpetual)\b', 100, 'ROLEX', 'vero'),
    (r'\b(omega|seamaster|speedmaster|constellation|planet.?ocean)\b', 100, 'OMEGA', 'vero'),
    (r'\b(patek.?philippe|patek|nautilus|aquanaut|calatrava)\b', 100, 'PATEK', 'vero'),
    (r'\b(audemars.?piguet|royal.?oak)\b', 100, 'AP', 'vero'),
    (r'\b(richard.?mille)\b', 100, 'RM', 'vero'),
    (r'\brm.?\d{2,3}\b', 90, 'RM_MODEL', 'vero'),
    (r'\b(cartier|cartier.?tank|santos|ballon.?bleu)\b', 100, 'CARTIER', 'vero'),
    (r'\b(hublot|big.?bang)\b', 100, 'HUBLOT', 'vero'),
    (r'\b(tag.?heuer|carrera|monaco|aquaracer)\b', 90, 'TAG', 'vero'),
    (r'\b(breitling|navitimer|superocean)\b', 90, 'BREITLING', 'vero'),
    (r'\b(iwc|portugieser|portofino)\b', 90, 'IWC', 'vero'),
    (r'\b(panerai|luminor|radiomir)\b', 90, 'PANERAI', 'vero'),
    (r'\b(tudor|black.?bay|pelagos)\b', 90, 'TUDOR', 'vero'),
    (r'\b(jaeger.?lecoultre|jlc|reverso)\b', 90, 'JLC', 'vero'),
    (r'\b(vacheron|chopard|zenith)\b', 90, 'LUXURY_WATCH', 'vero'),
    (r'\b(garmin|fitbit|amazfit|suunto|polar.?watch)\b', 80, 'SMARTWATCH_BRAND', 'vero'),
    (r'\b(disney|mickey|minnie|frozen|elsa|moana|cinderella|rapunzel)\b', 100, 'DISNEY', 'vero'),
    (r'\b(pixar|toy.?story|woody|buzz|nemo|incredibles|cars.?pixar)\b', 100, 'PIXAR', 'vero'),
    (r'\b(marvel|mcu|avengers?|iron.?man|spider.?man|hulk|thor)\b', 100, 'MARVEL', 'vero'),
    (r'\b(captain.?america|black.?panther|doctor.?strange|ant.?man)\b', 100, 'MARVEL', 'vero'),
    (r'\b(thanos|infinity.?gauntlet|wakanda|groot|deadpool|wolverine)\b', 100, 'MARVEL', 'vero'),
    (r'\b(dc.?comics|batman|superman|wonder.?woman|joker|harley.?quinn)\b', 100, 'DC', 'vero'),
    (r'\b(star.?wars|jedi|sith|darth|vader|yoda|mandalorian|lightsaber)\b', 100, 'STARWARS', 'vero'),
    (r'\b(stormtrooper|millennium.?falcon|death.?star|boba.?fett|grogu)\b', 100, 'STARWARS', 'vero'),
    (r'\b(harry.?potter|hogwarts|gryffindor|slytherin|hufflepuff|ravenclaw)\b', 100, 'HARRYPOTTER', 'vero'),
    (r'\b(dumbledore|hermione|voldemort|snape|deathly.?hallows|quidditch)\b', 100, 'HARRYPOTTER', 'vero'),
    (r'\b(pokemon|pokémon|pikachu|charizard|pokeball|poke.?ball)\b', 100, 'POKEMON', 'vero'),
    (r'\b(sanrio|hello.?kitty|kuromi|cinnamoroll|my.?melody|pompompurin)\b', 100, 'SANRIO', 'vero'),
    (r'\b(studio.?ghibli|ghibli|totoro|spirited.?away|no.?face)\b', 100, 'GHIBLI', 'vero'),
    (r'\b(naruto|sasuke|one.?piece|luffy|dragon.?ball|goku|vegeta)\b', 90, 'ANIME', 'vero'),
    (r'\b(demon.?slayer|tanjiro|nezuko|attack.?on.?titan|jujutsu|gojo)\b', 90, 'ANIME', 'vero'),
    (r'\b(sailor.?moon|bleach.?anime|fullmetal|death.?note)\b', 90, 'ANIME', 'vero'),
    (r'\b(sonic.?hedgehog|minecraft|creeper|enderman|fortnite)\b', 90, 'GAME_IP', 'vero'),
    (r'\b(transformers|optimus.?prime|bumblebee|megatron|starscream|soundwave|ironhide)\b', 100, 'HASBRO', 'vero'),
    (r'\b(hasbro|nerf|my.?little.?pony|power.?rangers)\b', 100, 'HASBRO', 'vero'),
    (r'\b(optimus|autobots?|decepticons?|cybertron)\b', 100, 'TRANSFORMERS', 'vero'),
    (r'\b(transformation.?toy|transformation.?robot|transform.?robot)\b', 100, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(deformation|deformat|deform.?robot|deform.?toy)\b', 100, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(bmb|wei.?jiang|weijiang|jinbao|jin.?bao|baiwei|bai.?wei)\b', 100, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(mpm.?\d+)\b', 90, 'TRANSFORMERS_MODEL', 'vero'),
    (r'\b(ls.?\d+[a-z]?)\b', 90, 'TRANSFORMERS_MODEL', 'vero'),
    (r'\b(oversized.?robot|ko.?version|knock.?off)\b', 90, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(transformation|morphing).*(robot|mecha|toy|figure)\b', 90, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(robot|mecha|toy|figure).*(transformation|morphing)\b', 90, 'TRANSFORMERS_KNOCKOFF', 'vero'),
    (r'\b(mattel|barbie|hot.?wheels|fisher.?price)\b', 90, 'MATTEL', 'vero'),
    (r'\b(funko|pop!?.?vinyl|bobblehead)\b', 80, 'FUNKO', 'vero'),
    (r'\b(tmnt|ninja.?turtle|teenage.?mutant)\b', 90, 'TMNT', 'vero'),
    (r'\b(nfl|nba|mlb|nhl|fifa|uefa|premier.?league|champions.?league)\b', 90, 'SPORTS_LEAGUE', 'vero'),
    (r'\b(manchester|liverpool|arsenal|chelsea|barcelona|real.?madrid|juventus|bayern)\b', 80, 'SPORTS_TEAM', 'vero'),
    (r'\b(lakers|celtics|warriors|yankees|cowboys|patriots)\b', 80, 'SPORTS_TEAM', 'vero'),
    (r'\b(formula.?1|f1.?racing|red.?bull.?racing|mclaren.?f1)\b', 80, 'F1', 'vero'),
    (r'\b(wwe|ufc|aew)\b', 80, 'SPORTS_ENTERTAINMENT', 'vero'),
    (r'\b(mercedes|benz|amg|maybach|brabus)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(bmw|m.?sport|m.?power|alpina)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(audi|quattro|s.?line|e.?tron)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(porsche|cayenne|panamera|macan|taycan|boxster|carrera)\b', 90, 'AUTO_BRAND', 'vero'),
    (r'\b(ferrari|lamborghini|maserati|aston.?martin|bentley|rolls.?royce)\b', 90, 'AUTO_BRAND', 'vero'),
    (r'\b(jaguar|land.?rover|range.?rover|defender)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(volkswagen|vw|golf.?gti|tiguan)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(ford|mustang|raptor|bronco)\b', 70, 'AUTO_BRAND', 'vero'),
    (r'\b(toyota|lexus|honda|acura|nissan|infiniti)\b', 70, 'AUTO_BRAND', 'vero'),
    (r'\b(mazda|subaru|mitsubishi|hyundai|kia|genesis)\b', 70, 'AUTO_BRAND', 'vero'),
    (r'\b(chevrolet|chevy|corvette|camaro|dodge|challenger|charger|hellcat)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(jeep|wrangler|grand.?cherokee)\b', 80, 'AUTO_BRAND', 'vero'),
    (r'\b(harley.?davidson|harley|ducati|triumph|indian.?motorcycle)\b', 80, 'MOTO_BRAND', 'vero'),
    (r'\b(yamaha|kawasaki|suzuki|ktm|aprilia)\b', 70, 'MOTO_BRAND', 'vero'),
    (r'\b(la.?mer|charlotte.?tilbury|nars|mac.?cosmetic|urban.?decay)\b', 80, 'COSMETICS', 'vero'),
    (r'\b(fenty|kylie.?cosmetic|rare.?beauty|glossier|the.?ordinary)\b', 80, 'COSMETICS', 'vero'),
    (r'\b(tom.?ford|creed|aventus|baccarat.?rouge|maison.?francis)\b', 90, 'FRAGRANCE', 'vero'),
    (r'\b(le.?labo|santal.?33|byredo|diptyque|jo.?malone)\b', 90, 'FRAGRANCE', 'vero'),
    (r'\b(chanel.?no.?5|coco.?mademoiselle|dior.?sauvage|sauvage)\b', 90, 'FRAGRANCE', 'vero'),
    (r'\b(makita|dewalt|milwaukee|bosch|ryobi|festool|hilti)\b', 80, 'TOOL_BRAND', 'vero'),
    (r'\b(snap.?on|snap-on|wera|knipex|wiha|bahco)\b', 80, 'TOOL_BRAND', 'vero'),
    (r'\b(stanley.?cup|stanley.?tumbler|stanley.?quencher)\b', 90, 'STANLEY', 'vero'),
    (r'\b(yeti|yeti.?tumbler|yeti.?rambler|yeti.?cooler)\b', 90, 'YETI', 'vero'),
    (r'\b(hydroflask|hydro.?flask|contigo|camelbak|nalgene)\b', 80, 'DRINKWARE_BRAND', 'vero'),
    (r'\b(herbalife|doterra|young.?living|scentsy|tupperware|amway)\b', 80, 'MLM', 'vero'),
    (r'\b(smart.?watch|smartwatch|fitness.?tracker|fitness.?band|fitness.?watch)\b', 70, 'SMARTWATCH_CLONE', 'vero'),
    (r'\b(tws|tws.?earbuds|true.?wireless|wireless.?earbuds|wireless.?headphone)\b', 70, 'EARBUDS_CLONE', 'vero'),
    (r'\b(airpods?|air.?pods?|earpods?|ear.?pods?|pro.?pods)\b', 100, 'AIRPODS', 'vero'),
    (r'\b(beats|beats.?by|beats.?studio|beats.?solo|beats.?fit)\b', 90, 'BEATS', 'vero'),
    (r'\b(drone|drones|quadcopter|uav)\b', 70, 'DRONE', 'vero'),
    (r'\b(action.?cam|sports?.?cam)\b', 60, 'ACTION_CAM', 'vero'),
    (r'\b(phone.?mount|phone.?holder|cell.?phone.?mount|cell.?phone.?holder)\b', 80, 'PHONE_MOUNT', 'patent'),
    (r'\b(bike.?phone|bicycle.?phone|motorcycle.?phone|scooter.?phone)\b', 85, 'BIKE_PHONE_MOUNT', 'patent'),
    (r'\b(handlebar.?mount|handlebar.?holder|stem.?mount)\b', 80, 'HANDLEBAR_MOUNT', 'patent'),
    (r'\b(car.?phone.?mount|car.?phone.?holder|dashboard.?mount|windshield.?mount|vent.?mount)\b', 75, 'CAR_PHONE_MOUNT', 'patent'),
    (r'\b(bike|bicycle|cycling|mtb).*(light|lamp).*(horn|bell)\b', 90, 'BIKE_LIGHT_HORN', 'patent'),
    (r'\b(horn|bell).*(bike|bicycle|cycling|mtb).*(light|lamp)\b', 90, 'BIKE_LIGHT_HORN', 'patent'),
    (r'\b(bike|bicycle|cycling).*(horn|bell).*rechargeable\b', 85, 'BIKE_HORN', 'patent'),
    (r'\b(front.?light|bike.?light|bicycle.?light).*(horn|bell)\b', 90, 'BIKE_LIGHT_HORN', 'patent'),
    (r'\b(horn|bell).*(front.?light|bike.?light|bicycle.?light)\b', 90, 'BIKE_LIGHT_HORN', 'patent'),
    (r'\b\d+.?db.*(bike|bicycle|cycling)\b', 80, 'BIKE_HORN_DB', 'patent'),
    (r'\b(bike|bicycle|cycling).*\d+.?db\b', 80, 'BIKE_HORN_DB', 'patent'),
    (r'\b(shimano)\b', 80, 'SHIMANO', 'vero'),
    (r'\b(swarovski)\b', 90, 'SWAROVSKI', 'vero'),
    (r'\b(tiffany|tiffany.?co)\b', 100, 'TIFFANY', 'vero'),
    (r'\b(tommy.?hilfiger)\b', 90, 'TOMMY', 'vero'),
    (r'\b(ugg|uggs)\b', 90, 'UGG', 'vero'),
    (r'\b(levi.?s|levis|levi.?strauss)\b', 90, 'LEVIS', 'vero'),
    (r'\b(ray.?ban|rayban)\b', 90, 'RAYBAN', 'vero'),
    (r'\b(oakley|ray.?ban|luxottica)\b', 90, 'OAKLEY', 'vero'),
    (r'\b(quad.?lock|quadlock)\b', 100, 'QUADLOCK', 'vero'),
    (r'\b(lacoste|izod)\b', 90, 'LACOSTE', 'vero'),
    (r'\b(volkswagen|vw|audi|porsche|bentley|lamborghini|bugatti)\b', 80, 'VW_GROUP', 'vero'),
]
# ============================================================================
# COMPATIBILITY PATTERNS
# ============================================================================
_RAW_COMPATIBILITY_RULES = [
    (r'\bfor\s+(iphone|ipad|ipod|macbook|imac|apple|airpods?|airtag|magsafe|watch)\b', 90, 'FOR_APPLE', 'compatibility'),
    (r'\bfor\s+(samsung|galaxy|note|fold|flip|tab)\b', 85, 'FOR_SAMSUNG', 'compatibility'),
    (r'\bfor\s+(sony|playstation|ps[2345]|psp|xperia)\b', 85, 'FOR_SONY', 'compatibility'),
    (r'\bfor\s+(nintendo|switch|wii|3ds|gameboy)\b', 85, 'FOR_NINTENDO', 'compatibility'),
    (r'\bfor\s+(xbox|surface|microsoft)\b', 85, 'FOR_MICROSOFT', 'compatibility'),
    (r'\bfor\s+(google|pixel|chromecast|nest)\b', 80, 'FOR_GOOGLE', 'compatibility'),
    (r'\bfor\s+(dji|mavic|phantom|osmo|spark)\b', 85, 'FOR_DJI', 'compatibility'),
    (r'\bfor\s+(gopro|hero\d*)\b', 85, 'FOR_GOPRO', 'compatibility'),
    (r'\bfor\s+(dyson|v\d+|airwrap|supersonic)\b', 85, 'FOR_DYSON', 'compatibility'),
    (r'\bfor\s+(bose|quietcomfort|soundlink)\b', 80, 'FOR_BOSE', 'compatibility'),
    (r'\bfor\s+(xiaomi|redmi|poco|mi\s*band)\b', 80, 'FOR_XIAOMI', 'compatibility'),
    (r'\bfor\s+(huawei|honor|mate)\b', 80, 'FOR_HUAWEI', 'compatibility'),
    (r'\bfor\s+(oneplus|oppo|vivo|realme|lenovo)\b', 75, 'FOR_PHONE', 'compatibility'),
    (r'\bfor\s+(garmin|fitbit|amazfit|suunto|polar)\b', 80, 'FOR_SMARTWATCH', 'compatibility'),
    (r'\bfor\s+(canon|nikon|fujifilm|lumix|panasonic)\b', 80, 'FOR_CAMERA', 'compatibility'),
    (r'\bfor\s+(roomba|irobot|braava)\b', 80, 'FOR_IROBOT', 'compatibility'),
    (r'\bfor\s+(kindle|alexa|echo|fire)\b', 80, 'FOR_AMAZON', 'compatibility'),
    (r'\bfor\s+(oculus|meta\s*quest|quest\s*[23])\b', 80, 'FOR_VR', 'compatibility'),
    (r'\bfor\s+(rolex|omega|seiko|casio|citizen|orient|tissot|longines|tag|breitling|tudor|hamilton)\b', 85, 'FOR_WATCH', 'compatibility'),
    (r'\bfor\s+(bmw|mercedes|benz|audi|porsche|volkswagen|vw|toyota|honda|nissan|mazda|subaru|ford|chevrolet|chevy|dodge|jeep|tesla|hyundai|kia|volvo|jaguar|land\s*rover|range\s*rover|mini|fiat|alfa|peugeot|renault|citroen|opel|vauxhall|skoda|seat)\b', 80, 'FOR_AUTO', 'compatibility'),
    (r'\bfor\s+(harley|ducati|yamaha|kawasaki|suzuki|ktm|triumph|aprilia|honda\s*cb|bmw\s*gs)\b', 80, 'FOR_MOTO', 'compatibility'),
    (r'\bfor\s+(makita|dewalt|milwaukee|bosch|ryobi|festool|hilti|metabo|hitachi|black.?decker|craftsman|ridgid|kobalt)\b', 80, 'FOR_TOOL', 'compatibility'),
    (r'\bfor\s+(kitchenaid|cuisinart|vitamix|ninja|instant\s*pot|nespresso|keurig|breville|delonghi|smeg|miele|bosch|siemens|lg|whirlpool|electrolux|samsung)\b', 80, 'FOR_APPLIANCE', 'compatibility'),
    (r'\bfor\s+(bell|shoei|arai|hjc|agv|shark|icon|scorpion|stilo|simpson|sparco|omp|klim|schuberth|nolan|x.?lite|caberg|ls2|mt.?helmets?)\b', 85, 'FOR_HELMET', 'compatibility'),
    (r'\bfor\s+(shimano|sram|campagnolo|fox|rockshox|specialized|trek|giant|cannondale)\b', 80, 'FOR_BIKE', 'compatibility'),
    (r'\b(compatible.?with|fits|works.?with|replacement.?for|designed.?for)\b', 50, 'COMPAT_CLAIM', 'compatibility'),
    (r'\b(oem|genuine|original|authentic)\b', 50, 'AUTH_CLAIM', 'compatibility'),
    (r'\b(aftermarket|third.?party|non.?oem)\b', 40, 'AFTERMARKET', 'compatibility'),
]
_RAW_LUXURY_COMBOS = [
    (r'\b(designer|luxury|premium|high.?end)\s+(bag|handbag|purse|wallet|belt|watch|sunglasses?|shoes?|sneakers?|hoodie|jacket|coat|scarf|hat)\b', 80, 'LUXURY_PRODUCT_COMBO', 'counterfeit'),
    (r'\b(branded|brand.?name|brand.?new.?style)\s+(bag|watch|shoe|sneaker)\b', 70, 'BRANDED_PRODUCT', 'counterfeit'),
    (r'\b(clover|four.?leaf|five.?leaf|alhambra|van.?cleef|vancleef|mother.?of.?pearl)\s*(bracelet|necklace|pendant|earring|ring)\b', 90, 'VCA_CLONE', 'counterfeit'),
]
_RAW_FANDOM_RULES = [
    (r'\b(cosplay|costume|cos)\s+(wig|outfit|armor|armour|weapon|sword|cape|mask|helmet|shield|glove|boot)\b', 70, 'COSPLAY_IP', 'vero'),
    (r'\b(fan.?art|fan.?made|fan.?fiction|fan.?merch)\b', 60, 'FAN_MERCH', 'vero'),
    (r'\b(anime|manga|otaku|weeb)\s+(figure|figurine|poster|sticker|keychain|plush|shirt|hoodie)\b', 70, 'ANIME_MERCH', 'vero'),
    (r'\b(superhero|super.?hero)\s+(figure|costume|mask|cape|toy|shirt)\b', 70, 'SUPERHERO_MERCH', 'vero'),
]
# ============================================================================
# PRECOMPILE ALL REGEX AT MODULE LOAD
# ============================================================================
def _compile_rules(raw_rules):
    return [(re.compile(p, re.IGNORECASE), s, n, c) for p, s, n, c in raw_rules]
HARD_BLOCK_RULES = _compile_rules(_RAW_HARD_BLOCK_RULES)
BRAND_RULES = _compile_rules(_RAW_BRAND_RULES)
COMPATIBILITY_RULES = _compile_rules(_RAW_COMPATIBILITY_RULES)
LUXURY_COMBO_RULES = _compile_rules(_RAW_LUXURY_COMBOS)
FANDOM_RULES = _compile_rules(_RAW_FANDOM_RULES)
_MODEL_PATTERN = re.compile(r'\b[A-Z]{1,4}[-.]?\d{2,5}[A-Z]{0,3}\b')
_CHASSIS_PATTERN = re.compile(r'\b(E[0-9]{2}|F[0-9]{2}|G[0-9]{2}|B[0-9]|MK[0-9]|W[12][0-9]{2}|C[0-9]{3}|X[0-9]{2,3})\b', re.IGNORECASE)
_TRADEMARK_SYMBOLS = re.compile(r'[®™©℠]')
_TITLECASE_TOKEN = re.compile(r'\b([A-Z][a-z]{2,}(?:[A-Z][a-z]+)*)\b')
_CAMELCASE_TOKEN = re.compile(r'\b([a-z]+[A-Z][a-zA-Z]*)\b')
_ALLCAPS_TOKEN = re.compile(r'\b([A-Z]{3,})\b')
COMPAT_WORDS = frozenset([
    'for', 'fits', 'compatible', 'replacement', 'works', 'designed',
    'suitable', 'oem', 'genuine', 'original', 'authentic', 'aftermarket',
    'accessory', 'accessories', 'attachment', 'part', 'parts', 'spare',
    'adapter', 'adaptor', 'connector', 'mount', 'bracket', 'case', 'cover',
    'sleeve', 'skin', 'protector', 'guard', 'holder', 'charger', 'dock',
    'cable', 'strap', 'band', 'tip', 'nib', 'filter', 'cartridge', 'refill',
])
_VERO_BRANDS = set()
def load_vero_dataset(path='vero_dataset.json'):
    global _VERO_BRANDS
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            for entry in data:
                for term in entry.get('brand_terms', []):
                    _VERO_BRANDS.add(term.lower().strip())
            return True
        except Exception:
            pass
    return False
_VERO_LOADED = load_vero_dataset()
def normalize_variants(text):
    if not text:
        return {'orig': '', 'norm': '', 'norm_sep': '', 'alnum': '', 'nospace': ''}
    orig = text
    t = text.lower()
    t = unicodedata.normalize('NFKD', t)
    for old, new in OBFUSCATION_MAP.items():
        t = t.replace(old, new)
    t = re.sub(r'[®™©℠]', '', t)
    norm = re.sub(r'\s+', ' ', t).strip()
    norm_sep = re.sub(r'[^a-z0-9\s]', ' ', norm)
    norm_sep = re.sub(r'\s+', ' ', norm_sep).strip()
    def join_spaced(s):
        def replacer(m):
            letters = m.group(0).strip()
            if all(len(p) == 1 for p in letters.split()):
                return ' ' + letters.replace(' ', '') + ' '
            return m.group(0)
        return re.sub(r'(?:^|\s)((?:\w\s+){2,}\w)(?:\s|$)', replacer, s)
    norm_sep = join_spaced(norm_sep)
    norm_sep = re.sub(r'\s+', ' ', norm_sep).strip()
    alnum = re.sub(r'[^a-z0-9 ]', '', norm_sep)
    alnum = re.sub(r'\s+', ' ', alnum).strip()
    nospace = alnum.replace(' ', '')
    return {'orig': orig, 'norm': norm, 'norm_sep': norm_sep, 'alnum': alnum, 'nospace': nospace}
def detect_brand_like_tokens(orig_text):
    signals = []
    if not orig_text:
        return signals
    if _TRADEMARK_SYMBOLS.search(orig_text):
        signals.append(('TRADEMARK_SYMBOL', orig_text))
    for m in _TITLECASE_TOKEN.finditer(orig_text):
        token = m.group(1)
        if token.lower() not in COMMON_WORDS and len(token) > 2:
            signals.append(('TITLECASE_TOKEN', token))
    for m in _CAMELCASE_TOKEN.finditer(orig_text):
        signals.append(('CAMELCASE_TOKEN', m.group(1)))
    for m in _ALLCAPS_TOKEN.finditer(orig_text):
        token = m.group(1)
        if token.lower() not in COMMON_WORDS and len(token) > 2:
            signals.append(('ALLCAPS_TOKEN', token))
    for m in _MODEL_PATTERN.finditer(orig_text):
        signals.append(('MODEL_PATTERN', m.group(0)))
    for m in _CHASSIS_PATTERN.finditer(orig_text):
        signals.append(('CHASSIS_PATTERN', m.group(0)))
    return signals
def check_allowlist(variants):
    text = variants['alnum']
    for allowed in ALLOWLIST_CATEGORIES:
        if allowed in text:
            return True, allowed
    return False, None
def assess_title(title, description='', brand='', mpn='', category=''):
    if not title or not title.strip():
        return Decision(False, 100, ['EMPTY_TITLE'], [], 'Empty title blocked.')
    score = 0
    reasons = []
    matches = []
    tv = normalize_variants(title)
    all_text = f"{title} {description} {brand} {mpn}".strip()
    av = normalize_variants(all_text)
    scan_texts = [tv['orig'].lower(), tv['norm'], tv['norm_sep'], tv['alnum'], tv['nospace']]
    is_helmet_visor = False
    for st in scan_texts:
        if _HELMET_VISOR_PATTERN.search(st):
            is_helmet_visor = True
            break
    if brand and brand.strip():
        brand_lower = brand.strip().lower()
        if brand_lower not in SAFE_BRAND_VALUES:
            if not is_helmet_visor:
                score = max(score, 100)
                reasons.append('BRAND_FIELD_PRESENT')
                matches.append(f'brand={brand.strip()}')
    if _VERO_BRANDS and not is_helmet_visor:
        for vb in _VERO_BRANDS:
            for st in scan_texts:
                if vb in st:
                    score = max(score, 100)
                    reasons.append('VERO_DATASET_MATCH')
                    matches.append(vb)
                    break
    for compiled, rscore, rname, rcat in HARD_BLOCK_RULES:
        if is_helmet_visor and rname in ('LOGO_PROJECTOR', 'CAR_BADGE', 'PHONE_MOUNT',
                                          'BIKE_PHONE_MOUNT', 'HANDLEBAR_MOUNT',
                                          'CAR_PHONE_MOUNT', 'DROP_STOP_PATENT'):
            continue
        for st in scan_texts:
            if compiled.search(st):
                score = max(score, rscore)
                reasons.append(f'{rcat}:{rname}')
                matches.append(rname)
                break
    if not is_helmet_visor:
        for compiled, rscore, rname, rcat in BRAND_RULES:
            for st in scan_texts:
                if compiled.search(st):
                    score = max(score, rscore)
                    reasons.append(f'{rcat}:{rname}')
                    matches.append(rname)
                    break
        for compiled, rscore, rname, rcat in BRAND_RULES:
            if compiled.search(tv['nospace']):
                if rname not in matches:
                    score = max(score, rscore)
                    reasons.append(f'{rcat}:{rname}(nospace)')
                    matches.append(rname)
    if not is_helmet_visor:
        for compiled, rscore, rname, rcat in LUXURY_COMBO_RULES:
            for st in scan_texts:
                if compiled.search(st):
                    score = max(score, rscore)
                    reasons.append(f'{rcat}:{rname}')
                    matches.append(rname)
                    break
    if not is_helmet_visor:
        for compiled, rscore, rname, rcat in FANDOM_RULES:
            for st in scan_texts:
                if compiled.search(st):
                    score = max(score, rscore)
                    reasons.append(f'{rcat}:{rname}')
                    matches.append(rname)
                    break
    has_compat = False
    if not is_helmet_visor:
        for compiled, rscore, rname, rcat in COMPATIBILITY_RULES:
            for st in scan_texts:
                if compiled.search(st):
                    has_compat = True
                    score = max(score, rscore)
                    reasons.append(f'{rcat}:{rname}')
                    matches.append(rname)
                    break
    if not is_helmet_visor:
        brand_tokens = detect_brand_like_tokens(title)
        has_brand_tokens = len(brand_tokens) > 0
        if has_brand_tokens:
            for btype, bval in brand_tokens[:5]:
                reasons.append(f'BRAND_LIKE:{btype}={bval}')
                matches.append(f'{btype}:{bval}')
        if has_brand_tokens and has_compat:
            score = max(score, 90)
            reasons.append('BRAND_TOKEN_PLUS_COMPAT')
    is_allowed, allowed_cat = check_allowlist(tv)
    if not is_helmet_visor and has_compat and score >= 60:
        score = max(score, 90)
        if 'COMPAT_BRAND_BLOCK' not in reasons:
            reasons.append('COMPAT_BRAND_BLOCK')
    if is_helmet_visor:
        reasons.insert(0, 'HELMET_VISOR_EXEMPT')
        matches.insert(0, 'HELMET_VISOR')
    if is_allowed and score < 30:
        allowed = True
        explanation = f"Generic item in allowlist category: {allowed_cat}"
    elif is_helmet_visor and score < CONFIG['block_threshold']:
        allowed = True
        explanation = f"Helmet visor exempt. Score {score}."
    elif score >= CONFIG['block_threshold']:
        allowed = False
        top_reason = reasons[0] if reasons else 'UNKNOWN'
        explanation = f"Blocked ({top_reason}). Score {score}."
    else:
        allowed = True
        explanation = f"Passed with score {score}."
    if score >= CONFIG['block_threshold']:
        allowed = False
    return Decision(allowed=allowed, score=score, reasons=reasons[:10], matches=matches[:10], explanation=explanation)
def find_column(fieldnames, *keywords):
    for col in fieldnames:
        for kw in keywords:
            if kw in col.lower():
                return col
    return None
def process_csv(input_file, config=None):
    if config is None:
        config = CONFIG
    base = os.path.splitext(input_file)[0]
    with open(input_file, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        items = list(reader)
    title_col = find_column(fieldnames, 'title', 'name') or fieldnames[0]
    desc_col = find_column(fieldnames, 'desc')
    brand_col = find_column(fieldnames, 'brand')
    mpn_col = find_column(fieldnames, 'mpn', 'model', 'part')
    cat_col = find_column(fieldnames, 'categ', 'type')
    safe, blocked, greylist = [], [], []
    stats = defaultdict(int)
    top_triggers = defaultdict(int)
    for item in items:
        title = item.get(title_col, '')
        desc = item.get(desc_col, '') if desc_col and config.get('scan_description') else ''
        brand = item.get(brand_col, '') if brand_col else ''
        mpn = item.get(mpn_col, '') if mpn_col else ''
        cat = item.get(cat_col, '') if cat_col else ''
        dec = assess_title(title, desc, brand, mpn, cat)
        item['_decision'] = 'BLOCK' if not dec.allowed else 'SAFE'
        item['_risk_score'] = dec.score
        item['_blocked_reason'] = '; '.join(dec.reasons[:5])
        item['_blocked_rule'] = dec.reasons[0] if dec.reasons else ''
        item['_matched_terms'] = ', '.join(dec.matches[:5])
        item['_explanation'] = dec.explanation
        if not dec.allowed:
            blocked.append(item)
            stats['blocked'] += 1
            for r in dec.reasons[:3]:
                top_triggers[r] += 1
        elif dec.score >= config.get('greylist_threshold', 15):
            greylist.append(item)
            stats['greylist'] += 1
        else:
            safe.append(item)
            stats['safe'] += 1
    stats['total'] = len(items)
    out_fields = fieldnames + ['_decision','_risk_score','_blocked_reason','_blocked_rule','_matched_terms','_explanation']
    with open(f'{base}_SAFE.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for item in safe:
            w.writerow({k: item.get(k,'') for k in fieldnames})
    with open(f'{base}_BLOCKED.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(blocked)
    with open(f'{base}_GREYLIST.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(greylist)
    report = {
        'generated': datetime.now().isoformat(),
        'stats': dict(stats),
        'block_rate': f"{stats['blocked']/max(stats['total'],1)*100:.1f}%",
        'top_triggers': dict(sorted(top_triggers.items(), key=lambda x: x[1], reverse=True)[:30]),
    }
    with open(f'{base}_REPORT.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)
    return stats, report
def main():
    parser = argparse.ArgumentParser(description='MAX SAFETY FILTER v5.7')
    parser.add_argument('input', nargs='?', help='Input CSV file')
    parser.add_argument('--relaxed', action='store_true')
    parser.add_argument('--allowlist', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--no-description', action='store_true')
    args = parser.parse_args()
    config = CONFIG.copy()
    if args.relaxed:
        config['mode'] = 'relaxed'
        config['block_threshold'] = 60
        config['greylist_threshold'] = 40
    if args.allowlist:
        config['allowlist_only'] = True
    if args.no_description:
        config['scan_description'] = False
    if not args.input:
        print("MAX SAFETY FILTER v5.7 - PARANOID EBAY UK COMPLIANCE")
        print("Usage: python3 ebay_filter.py input.csv")
        sys.exit(0)
    if not os.path.exists(args.input):
        print(f"Error: File not found: {args.input}")
        sys.exit(1)
    print(f"\nMAX SAFETY FILTER v5.7 - Mode: {config['mode'].upper()}")
    stats, report = process_csv(args.input, config)
    base = os.path.splitext(args.input)[0]
    t = max(stats['total'], 1)
    print(f"Total: {stats['total']}")
    print(f"  SAFE: {stats['safe']} ({stats['safe']/t*100:.1f}%)")
    print(f"  GREYLIST: {stats.get('greylist',0)} ({stats.get('greylist',0)/t*100:.1f}%)")
    print(f"  BLOCKED: {stats['blocked']} ({stats['blocked']/t*100:.1f}%)")
    print(f"\nOutputs: {base}_SAFE.csv, {base}_BLOCKED.csv, {base}_GREYLIST.csv, {base}_REPORT.json")
if __name__ == "__main__":
    main()
