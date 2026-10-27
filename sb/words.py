"""Word list for recovery phrases. Order doesn't matter (phrases are hashed),
but the list must stay >= 256 unique words so 12 words give >= 96 bits."""

WORDS = """
acid acorn actor agent alarm album alley amber angel ankle apple apron arena
arrow aspen atlas attic autumn badge bagel baker bamboo banjo barley basin beach
beacon beaver berry bison blade blanket blaze bloom bonsai border bottle bramble
brick bridge bronze brook bubble bucket buffalo bugle butter cabin cactus camel
canal candle canoe canyon carbon cargo carpet castle cedar cello chalk cherry
chess chimney cider cinema circus citrus clover cobalt cocoa comet coral cotton
cougar coyote crane crater crayon cricket crystal cupcake cypress dagger daisy
delta denim desert diamond dingo dolphin domino dragon drum eagle easel echo
eclipse elbow ember emerald engine falcon feather fern ferry fiddle flame
flannel flute fossil fox galaxy garden garlic gazelle geyser ginger glacier
globe goblet gopher granite grape gravel guitar hammer harbor harp hazel helmet
heron hickory honey hornet husky igloo iris island ivory jacket jaguar jasmine
jelly jigsaw juniper kayak kettle kiwi koala ladder lagoon lantern lava lemon
lily lizard llama lobster locket lotus lumber lynx magnet mango maple marble
meadow melon meteor mint mitten monkey moose mosaic muffin nectar needle nickel
nutmeg oasis ocean olive onion orbit orchid otter oyster paddle panda papaya
parrot peach pebble pecan pepper pickle pigeon pillow pine pirate pixel planet
plum pocket pony poppy prism pumpkin puzzle quail quartz quill rabbit radar
radish raven reef ribbon river robin rocket rose ruby saddle salmon sapphire
satin scarf shadow shell sierra silver slate sloth snail sparrow spider spruce
squash staple stone summit sunset swan tablet tango temple thistle thunder
tiger timber toast topaz tulip tundra turtle tuxedo umbrella valley velvet
violin volcano walnut walrus water whale willow window wizard yacht yarrow
zebra zipper anchor basil biscuit blossom cabbage caramel cashew cobra compass
cookie copper falafel fjord gecko goose hamster hedgehog iguana jungle kernel
lantana mammoth marlin mustard narwhal noodle orange osprey panther pelican
penguin pistachio pretzel puffin raisin sequoia sesame sorbet starfish tofu
truffle waffle wombat yogurt
""".split()

assert len(set(WORDS)) == len(WORDS) >= 256, "word list must be >= 256 unique words"
