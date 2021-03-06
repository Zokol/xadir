import os, sys, time
import pygame
import math
import random
import Image
from pygame.locals import *
from helpers import *
from resources import *
from grid import *
from algo import *
from UI import *

from dice import Dice
from race import races, Race
from armor import armors, Armor, default as default_armor
from weapon import weapons, Weapon, default as default_weapon
from charclass import classes, win_xp, defeat_xp, CharacterClass
from character import Character
from charsprite import CharacterSprite
from terrain import terrains
from taunts import taunts

from tiles import *
from bgmap import BackgroundMap
from pixelfont import *

from wire import *
from messager import Messager
import socket
import asyncore

from resolver import *

if not pygame.font:
	print "Warning: Fonts not enabled"
if not pygame.mixer:
	print "Warning: Audio not enabled"

def get_hp_bar_color(total, value):
	"""Linearly generates colors from red to yellow to green"""
	# 256 values per color, so red-to-yellow and yellow-to-green make 512 - but yellow gets counted twice, so it's really 511
	num_colors = 511
	c = scale(value, total, num_colors)
	return (min(num_colors - c, 255), min(c, 255), 0)

# XXX: Possible optimization: Cache 100% health and do a partial blit instead of N+1 fills?
def draw_gradient_hp_bar(surface, rect, total, left):
	surface.fill((0, 0, 0), rect)
	for i in range(scale_ceil(left, total, rect.width)):
		color = get_hp_bar_color(rect.width - 1, i)
		surface.fill(color, (rect.x + i, rect.y, 1, rect.height))

def draw_solid_hp_bar(surface, rect, total, left):
	color = get_hp_bar_color(total, left)
	surface.fill(color, rect)

def draw_solid_hp_bar2(surface, rect, total, left):
	color = get_hp_bar_color(total, left)
	surface.fill((0, 0, 0), rect)
	surface.fill(color, (rect.x, rect.y, scale_ceil(left, total, rect.width), rect.height))

def get_hue_color(i):
	# red-yellow-green-cyan-blue-magenta-red
	# 6*256-6 = 1530

	#0 = red
	#255 = yellow
	#510 = green
	#765 = cyan
	#1020 = blue
	#1275 = magenta
	#1530 = red

	n = lambda c: max(min(c, 255), 0)

	r = n(abs(765-((i+0)%1530))-255)
	g = n(abs(765-((i+510)%1530))-255)
	b = n(abs(765-((i+1020)%1530))-255)

	return (r, g, b)

# Change to suit your mood
draw_main_hp_bar = draw_gradient_hp_bar
draw_char_hp_bar = draw_solid_hp_bar2

def pygame_surface_from_pil_image(im):
	if im.mode not in ('RGB', 'RGBA'):
		im = im.convert('RGBA')
	data = im.tostring()
	return pygame.image.fromstring(data, im.size, im.mode)

def get_animation_frames(path):
	anim = Image.open(path)
	try:
		while 1:
			yield anim
			# This is ugly, but PIL seems to corrupt all the
			# other frames of animation as soon as you do
			# *anything* with one of them. You can't even
			# copy() each frame, you've just got to open the
			# image again for each frame and iterate to the
			# correct position. (Yeah, you can't even use
			# one seek to get there...)
			end = anim.tell()
			anim = Image.open(path)
			start = anim.tell()
			for pos in range(start, end + 1):
				anim.seek(pos+1)
	except EOFError:
		pass # end of sequence

def get_animation_surfaces(path):
	for im in get_animation_frames(path):
		surface = pygame_surface_from_pil_image(im)
		rect = surface.get_rect()
		yield pygame.transform.scale(surface, (rect.width * SCALE, rect.height * SCALE))

def get_heading(a, b):
	delta = (b[0] - a[0], b[1] - a[1])
	# Negate y because screen coordinates differ from math coordinates
	angle = math.degrees(math.atan2(-delta[1], delta[0])) % 360
	# Make the angle an integer and a multiple of 45
	angle = int(round(angle / 45)) * 45
	# Prefer horizontal directions when the direction is not a multiple of 90
	return {45: 0, 135: 180, 225: 180, 315: 0}.get(angle, angle)

class NetworkGame:
	def __init__(self, game, lounge):
		self.game = game
		self.lounge = lounge

	def __getattr__(self, name):
		return getattr(self.game, name)

	def move(self, character, target_pos):
		self.lounge.move(self.all_players.index(character.player), character.player.all_characters.index(character), target_pos)
		return []

	def attack(self, character, target_pos):
		self.lounge.attack(self.all_players.index(character.player), character.player.all_characters.index(character), target_pos)
		return []

	def end_turn(self):
		self.lounge.end_turn()
		return []

class Game:
	def __init__(self, map, spawns, players):
		self.map = map
		self.spawns = spawns
		self.all_players = players

		self.walkable = ['G', 'D', 'F']

		self.turn = 0

	current_player = property(lambda self: self.all_players[self.turn])
	live_players = property(lambda self: [player for player in self.all_players if player.is_alive()])
	dead_players = property(lambda self: [player for player in self.all_players if not player.is_alive()])

	# Player actions

	def move(self, character, dst_pos):
		assert dst_pos in self.get_action_area_for(character)
		assert not self.is_attack_move(character, dst_pos)

		src_pos = character.grid_pos
		path = self.get_move_path_for(character, src_pos, dst_pos)

		return [('MOVE', self.all_players.index(character.player), character.player.all_characters.index(character), path)]

	def attack(self, character, target_pos):
		assert target_pos in self.get_action_area_for(character)
		assert self.is_attack_move(character, target_pos)

		src_pos = character.grid_pos
		path = self.get_attack_path_for(character, src_pos, target_pos)

		target = self.get_other_character_at(character.player, target_pos)
		damage, messages = roll_attack_damage(self.map, character, target)

		return [('ATTACK', self.all_players.index(character.player), character.player.all_characters.index(character), path, target.hp, damage, messages)]

	def end_turn(self):
		if len(self.all_players) < 1 or len(self.live_players) < 1:
			return

		turn = (self.turn + 1) % len(self.all_players)
		while not self.all_players[turn].is_alive():
			turn = (turn + 1) % len(self.all_players)

		return [('TURN', turn)]

	# Actual state updates

	def handle_move(self, player_idx, character_idx, path):
		character = self.all_players[player_idx].all_characters[character_idx]
		distance = len(path) - 1

		character.grid_pos = path[-1]
		character.mp -= distance
		character.heading = get_heading(path[-2], path[-1])

	def handle_attack(self, player_idx, character_idx, path, orig_hp, damage, messages):
		character = self.all_players[player_idx].all_characters[character_idx]
		target = self.get_other_character_at(character.player, path[-1])
		distance = len(path) - 2

		character.grid_pos = path[-2]
		character.mp = 0
		character.heading = get_heading(path[-2], path[-1])
		target.hp = orig_hp
		target.take_hit(damage)

	def handle_turn(self, turn):
		self.turn = turn
		self.current_player.reset_movement_points()

	# Support stuff

	def get_enemy_players(self, player):
		return [p for p in self.all_players if p != player]

	def get_enemy_characters(self, player):
		return [c for p in self.all_players for c in p.characters if p != player]

	def get_all_other_characters(self, character):
		return [c for p in self.all_players for c in p.characters if c != character]

	def get_enemy_character_positions(self, player):
		return [c.grid_pos for p in self.all_players for c in p.characters if p != player]

	def get_all_other_character_positions(self, character):
		return [c.grid_pos for p in self.all_players for c in p.characters if c != character]

	def get_all_own_character_positions(self, player):
		return [c.grid_pos for c in player.characters]

	def get_other_character_at(self, player, coords):
		target = None
		for p in self.get_enemy_players(player):
			for c in p.characters:
				if c.grid_pos == coords:
					target = c
		return target

	def is_attack_move(self, character, coords):
		for c in self.get_enemy_character_positions(character.player):
			if c == coords:
				return True
		return False

	# Map-related methods

	def is_walkable(self, coords):
		"""Is the terrain at this point walkable?"""
		grid = self.map
		return coords in grid and grid[coords] in self.walkable

	def is_passable_for(self, character, coords):
		"""Is it okay for <character> to pass through this point without stopping?"""
		return self.is_walkable(coords) and coords not in self.get_enemy_character_positions(character.player)

	def is_haltable_for(self, character, coords):
		"""Is it okay for <character> to stop at this point?"""
		return self.is_walkable(coords) and coords not in self.get_all_other_character_positions(character)

	def get_move_path_for(self, character, start, end):
		"""Get path from start to end; all the intermediate points will be passable and the last one haltable"""
		assert isinstance(start, tuple)
		assert isinstance(end, tuple)
		assert self.is_haltable_for(character, end)
		return shortest_path(self, start, end, lambda self_, pos: self_.get_neighbours(pos, lambda pos_: self_.is_passable_for(character, pos_)))

	def get_attack_path_for(self, character, start, end):
		"""Get path suitable for attacking from start to end; ie. a path where you can stop on the point just *before* the last one"""
		assert isinstance(start, tuple)
		assert isinstance(end, tuple)
		assert end in self.get_enemy_character_positions(character.player), 'Target square must contain enemy character'
		# Get possible stopping points, one square away from the enemy
		ends = self.get_neighbours(end, lambda pos: self.is_haltable_for(character, pos))
		path = shortest_path_any(self, start, set(ends), lambda self_, pos: self_.get_neighbours(pos, lambda pos_: self_.is_passable_for(character, pos_)))
		if path:
			path.append(end)
		return path

	def get_action_area_for(self, character):
		"""Get points where the character can either move or attack"""
		result = bfs_area(self, character.grid_pos, character.mp, lambda self_, pos: self_.get_neighbours(pos, lambda pos_: self_.is_walkable(pos_)) if self_.is_passable_for(character, pos) else [])
		# Remove own characters
		result = set(result) - set(self.get_all_own_character_positions(character.player))
		# Remove enemies that can't be reached
		result = result - set(c_pos for c_pos in self.get_enemy_character_positions(character.player) if not self.get_neighbours(c_pos, lambda pos: pos == character.grid_pos or pos in result))
		return list(result)

	def get_neighbours(self, coords, filter = None, size = 1):
		"""Get surrounding points, filtered by some function"""
		if filter is None:
			filter = lambda pos: True
		grid = self.map
		result = [pos for pos in grid.env_keys(coords, size) if filter(pos)]
		result.sort(key = lambda pos: get_distance_2(pos, coords))
		return result

	# ...

	def get_spawnpoints(self, teams):
		result = []
		player_ids = random.sample(self.spawns, len(teams))
		for player_id, team in zip(player_ids, teams):
			name, remote, characters = team
			spawn_points = random.sample(self.spawns[player_id], len(characters))
			result.append(spawn_points)
		return result

class XadirMain:
	"""Main class for initialization and mechanics of the game"""
	def __init__(self, screen, mapname='map_new.txt'):
		pygame.display.set_caption('Xadir')
		self.mapname = mapname
		self.screen = screen
		self.width, self.height = self.screen.get_size()
		self.background = pygame.Surface((self.width, self.height))
		self.background.fill((159, 182, 205))
		self.sidebar = pygame.Rect(960, 0, 240, 720)
		self.buttons = []
		self.clock = pygame.time.Clock()
		self.fps = FPS
		self.showhealth = False
		self.buttons.append(Button(980, 600, 200, 100, "End turn", 40, self.end_turn))

		self.disabled_chartypes = {}

		self.messages = Messages(980, 380, 200, 200)

		self.sprites = pygame.sprite.LayeredDirty(_time_threshold = 1000.0)
		self.sprites.set_clip()
		self.sprites.add(Fps(self.clock, self.sidebar.centerx))
		self.sprites.add(CurrentPlayerName(self, self.sidebar.centerx))
		self.sprites.add(self.buttons)
		self.sprites.add(self.messages)

		self.remote = None
		self.waiting_for_response = False

		self.res = Resources(None)

		self.chartypes = self.res.races
		self.imgs = self.res.selections

		map, mapsize, spawns = load_map(self.mapname)
		self.map = BackgroundMap(map, *mapsize, res = self.res)
		self.spawns = spawns

		self.game = Game(self.map, self.spawns, [])

		log_stats('game')

	def is_local_turn(self):
		return not self.game.current_player.remote and not self.waiting_for_response

	def poll_local_events(self):
		for event in pygame.event.get():
			if event.type == pygame.QUIT:
				sys.exit()
			if self.is_local_turn():
				if event.type == pygame.MOUSEBUTTONDOWN:
					if event.button == 1:
						for b in self.buttons:
							if b.contains(*event.pos):
								b.function()
						self.click()
				if event.type == KEYDOWN and event.key == K_SPACE:
					self.end_turn()

	def main_loop(self):
		self.init_sidebar()

		change_sound(0, load_sound('battle.ogg'), BGM_FADE_MS)

		self.done = False
		while not self.done:
			self.draw()
			asyncore.loop(count=1, timeout=0.0)
			self.poll_local_events()

			if self.is_local_turn():
				if self.game.current_player.movement_points_left() < 1:
					self.end_turn()

			if len(self.game.live_players) <= 1:
				self.gameover()

	def end_game(self):
		self.done = True

	def gameover(self):
		print "gameover"
		change_sound(0, load_sound('menu.ogg'), BGM_FADE_MS)
		if len(self.game.live_players) < 1:
			text = 'Draw!'
			for p in self.game.all_players:
				for c in p.all_characters:
					c.xp += win_xp
					c.check_lvl()
		else:
			text = '%s wins!' % self.game.live_players[0].name
			for c in self.game.live_players[0].all_characters:
				c.char.xp += win_xp
				c.check_lvl()
			for p in self.game.dead_players:
				for c in p.all_characters:
					c.char.xp += defeat_xp
					c.check_lvl()
		sprite = Textile(text, pygame.Rect((0, 0, 960, 720)), layer = L_GAMEOVER)
		self.sprites.add(sprite)
		self.sprites.remove(self.buttons)
		self.buttons = [Button(980, 600, 200, 100, "End game", 40, self.end_game, (185, 139, 139))]
		self.sprites.add(self.buttons)
		
		while not self.done:
			self.draw()
			for event in pygame.event.get():
				if event.type == pygame.MOUSEBUTTONDOWN:
					if event.button == 1:
						for b in self.buttons:
							if b.contains(*event.pos):
								b.function()
				if event.type == pygame.QUIT:
					sys.exit()

	def draw(self, frames = 1):
		for i in range(frames):
			self.clock.tick(self.fps)
			self.sprites.update()
			self.sprites.clear(self.screen, self.background)
			# Update layers
			self.sprites._spritelist.sort(key = lambda sprite: sprite._layer)
			self.sprites.draw(self.screen)
			pygame.display.flip()

	def init_teams(self, teams, spawns):
		self.remote = None
		for (name, remote, characters), spawn in zip(teams, spawns):
			if remote:
				remote.handle_message = self.handle_remote
				self.remote = remote
			self.add_player(name, remote, [(char, x, y, 0) for char, (x, y) in zip(characters, spawn)])

		self.turn = 0
		self.grid_sprites = pygame.sprite.Group()
		self.map_sprites = self.map.sprites.values()
		self.sprites.add(self.map_sprites)
		for p in self.game.all_players:
			self.sprites.add(p.all_characters)
			for c in p.all_characters:
				self.sprites.add(DisabledCharacter(self, c))

	def set_grid_sprites(self, sprites):
		self.sprites.remove(self.grid_sprites)
		self.grid_sprites = sprites
		self.sprites.add(self.grid_sprites)

	def click(self):
		mouse_pos = pygame.mouse.get_pos()
		mouse_grid_pos = (mouse_pos[0]/TILE_SIZE[0], mouse_pos[1]/TILE_SIZE[1])
		player = self.game.current_player
		for character in player.characters:
			if character.grid_pos == mouse_grid_pos:
				if character.is_selected():
					character.unselect()
					self.set_grid_sprites(pygame.sprite.Group())
				else:
					character.select()
					if character.mp <= 0:
						self.movement_grid = SpriteGrid([character.grid_pos], self.imgs['red'])
						self.set_grid_sprites(self.movement_grid.sprites)
					else:
						self.movement_grid = SpriteGrid(self.game.get_action_area_for(character), self.imgs['green'])
						self.set_grid_sprites(self.movement_grid.sprites)
			elif character.is_selected():
				self.set_grid_sprites(pygame.sprite.Group())
				character.unselect()
				if mouse_grid_pos in self.game.get_action_area_for(character):
					if self.game.is_attack_move(character, mouse_grid_pos):
						self.attack(character, mouse_grid_pos)
					else:
						self.move(character, mouse_grid_pos)
			if character.grid_pos != mouse_grid_pos:
				character.unselect()

	def move(self, character, coords):
		self.waiting_for_response = True
		self.handle_actions(self.game.move(character, coords))

	def attack(self, character, coords):
		self.waiting_for_response = True
		self.handle_actions(self.game.attack(character, coords))

	def end_turn(self):
		self.waiting_for_response = True
		self.handle_actions(self.game.end_turn())

	def handle_remote(self):
		pass

	def handle_actions(self, actions):
		for action in actions:
			self.handle_action(action)

	def handle_action(self, action):
		assert action[0] in 'TURN MOVE ATTACK'.split()
		handler = getattr(self, 'handle_' + action[0].lower())
		handler(*action[1:])

	def handle_turn(self, player_idx):
		self.game.handle_turn(player_idx)
		self.messages.messages.append('%s\'s turn' % self.game.current_player.name)
		self.waiting_for_response = False

	def handle_move(self, player_idx, character_idx, path):
		character = self.game.all_players[player_idx].all_characters[character_idx]
		self.animate_move(character, path)
		self.game.handle_move(player_idx, character_idx, path)
		self.waiting_for_response = False

	def handle_attack(self, player_idx, character_idx, path, old_hp, damage, messages):
		character = self.game.all_players[player_idx].all_characters[character_idx]
		target = self.game.get_other_character_at(character.player, path[-1])
		self.animate_move(character, path[:-1])
		character.heading = get_heading(path[-2], path[-1])
		self.animate_attack(character, target, damage, messages)
		self.game.handle_attack(player_idx, character_idx, path, old_hp, damage, messages)
		self.waiting_for_response = False

	def animate_move(self, character, path):
		# Five steps per second
		frames = self.fps / 5
		for i in range(1, len(path) - 1):
			character.heading = get_heading(path[i], path[i+1])
			character.grid_pos = path[i]
			self.draw(frames)
		# This would come from the loop, but if there's only one step it doesn't, since the first turning isn't animated
		if len(path) > 1:
			character.heading = get_heading(path[-2], path[-1])
			character.grid_pos = path[-1]
			self.draw(frames)

	def animate_attack(self, attacker, target, damage, messages):
		self.animate_taunt(attacker)
		self.animate_hit(target, os.path.join(GFXDIR, "sword_hit_small.gif"))
		self.messages.messages.append(' '.join(messages))
		if damage:
			self.animate_hp_change(target, -damage)

	def animate_taunt(self, character):
		texts = taunts[None] + taunts.get(character.race.name, [])
		text = random.choice(texts)

		image = draw_speech_bubble(text)
		rect = image.get_rect()
		rect.topleft = character.pos
		rect.top -= rect.height

		sprite = Tile(image, rect, layer=(4,))

		self.sprites.add(sprite)
		self.draw(FPS)
		self.sprites.remove(sprite)

	def animate_hit(self, character, file_path):
		anim = AnimatedEffect(character, file_path, FPS / HIT_FPS)

		self.sprites.add(anim)

		while anim.visible:
			self.draw()

		self.sprites.remove(anim)

	def animate_hp_change(self, character, change):
		# Set up damage notification
		text = DamageNotification(character, change)

		self.sprites.add(text)

		# Animate hp change (and damage notification)
		change_sign = sign(change)
		change_amount = abs(change)
		orig_hp = character.hp
		for i in range(1, FPS):
			character.hp = orig_hp + change_sign * scale(i, FPS, change_amount)
			self.draw()
			if character.hp <= 0:
				break

		# Finish damage notification animation
		while text.visible:
			self.draw()

		# Clean up damage notification
		self.sprites.remove(text)

		# Clean up hp change
		character.hp = orig_hp

	def add_player(self, name, remote, characters):
		self.game.all_players.append(GamePlayer(name, characters, self, remote))

	def opaque_rect(self, rect, color=(0, 0, 0), opaque=255):
		box = pygame.Surface((rect.width, rect.height)).convert()
		box.fill(color)
		box.set_alpha(opaque)
		return [box, (rect.left, rect.top)]

	def init_sidebar(self):
		coords = [(self.sidebar.left + 10), (self.sidebar.top + 100)]
		width = self.sidebar.width - 20
		height = self.sidebar.height - 110
		bar_height = 20
		margin = 5
		bar_size = [width, bar_height]
		players = self.game.all_players
		for player in players:
			label = PlayerName(player, pygame.Rect(coords, (1, 1)))
			self.sprites.add(label)
			coords[1] += label.font.get_linesize()
			for character in player.all_characters:
				character_healthbar_rect = pygame.Rect(tuple(coords), tuple(bar_size))
				bar = MainHealthBar(character, character_healthbar_rect)
				self.sprites.add(bar)
				# XXX: fix character healthbars
				#if self.showhealth:
				#	draw_char_hp_bar(self.screen, pygame.Rect((character.x + 2, character.y - (CHAR_SIZE[1] - TILE_SIZE[1])), (48-4, 8)), character.max_hp, character.hp)
				coords[1] += (bar_height + margin)

class Messages(StateTrackingSprite):
	def __init__(self, x, y, width, height):
		StateTrackingSprite.__init__(self)

		self.image = pygame.Surface((width, height))
		self.rect = pygame.Rect((x, y, width, height))

		self.font = pygame.font.Font(FONT, int(16*FONTSCALE))

		self.messages = []

	def wrap_line(self, line):
		words = line.split()

		wrapped = []
		while words:
			fit, words = self.find_max_fit(words)
			if fit:
				wrapped.append(' '.join(fit))
			else:
				fit, rest = self.find_max_fit(words[0])
				wrapped.append(fit)
				words[0] = rest

		return wrapped

	def find_max_fit(self, words):
		fits, cuts = 0, len(words) + 1
		while fits + 1 < cuts:
			index = (fits + cuts) / 2
			if isinstance(words, list):
				partial = ' '.join(words[:index])
			else:
				partial = words[:index]
			width, height = self.font.size(partial)
			if width > self.rect.width:
				cuts = index
			else:
				fits = index
		return words[:fits], words[fits:]

	def cull_messages(self):
		linesize = self.font.get_linesize()
		num_lines = self.rect.height / linesize

		messages = []
		for message in self.messages:
			messages.extend(self.wrap_line(message))

		self.messages = messages[-num_lines:]

	def get_state(self):
		self.cull_messages()
		return self.messages

	def redraw(self):
		self.image.fill((127, 127, 127))
		y = 0
		for message in self.messages:
			text = self.font.render(message, True, (0, 0, 0))
			self.image.blit(text, (0, y))
			y += self.font.get_linesize()

class AnimatedEffect(AnimatedSprite):
	def __init__(self, character, file_path, interval = 1):
		images = list(get_animation_surfaces(file_path))
		rect = character.rect
		layer = L_CHAR_EFFECT(character.grid_y)

		AnimatedSprite.__init__(self, images, rect, layer, interval)

	def update(self):
		AnimatedSprite.update(self)

		if self.pos == 0 and self.count == 0:
			self.visible = 0

class DamageNotification(pygame.sprite.DirtySprite):
	def __init__(self, character, number, step=1, interval=2):
		pygame.sprite.DirtySprite.__init__(self)

		self.character = character
		self.number = number
		self.step = SCALE

		self.image = draw_pixel_text(str(self.number), SCALE, (255, 0, 0) if number < 0 else (0, 255, 0))
		self.rect = self.image.get_rect()
		self.rect.topleft = character.pos
		self.rect.top -= 5
		self.rect.left += (CHAR_SIZE[0] - self.rect.width) / 2

		self._layer = L_CHAR_EFFECT(character.grid_y)

		self.image.set_alpha(255)

		self.pos = 0
		self.opaque_height = 15
		self.max_height = 20

		self.count = 0
		self.interval = interval

	def update(self):
		if not self.visible:
			return

		self.count += 1
		if self.count >= self.interval:
			self.count = 0
			self.pos += 1
			if self.pos >= self.max_height:
				self.visible = 0

			self.rect.top -= self.step

			if self.pos >= self.opaque_height and self.opaque_height != self.max_height:
				next_alpha = 255 - ((255 * (self.pos - self.opaque_height)) / (self.max_height - self.opaque_height))
				self.image.set_alpha(next_alpha)

			self.dirty = 1

class MainHealthBar(StateTrackingSprite):
	def __init__(self, character, rect):
		StateTrackingSprite.__init__(self)
		self.character = character

		self.image = pygame.Surface(rect.size)
		self.rect = rect

		self.font = pygame.font.Font(FONT, int(20*FONTSCALE))

	def get_state(self):
		return self.character.max_hp, self.character.hp

	def redraw(self):
		draw_main_hp_bar(self.image, self.image.get_rect(), self.character.max_hp, self.character.hp)

		text = self.font.render('%d/%d' % (self.character.hp, self.character.max_hp), True, (127, 127, 255))
		rect = text.get_rect()
		pos = ((self.rect.width - rect.width) / 2, (self.rect.height - rect.height) / 2)
		self.image.blit(text, pos)

class PlayerName(pygame.sprite.DirtySprite):
	def __init__(self, player, rect):
		pygame.sprite.DirtySprite.__init__(self)
		self.player = player

		self.font = pygame.font.Font(FONT, int(20*FONTSCALE))

		self.image = self.font.render(self.player.name, True, (255,255, 255))
		self.rect = self.image.get_rect()
		self.rect.topleft = rect.topleft

class CurrentPlayerName(StateTrackingSprite):
	def __init__(self, main, centerx):
		StateTrackingSprite.__init__(self)
		self.centerx = centerx
		self.main = main

		self.font = pygame.font.Font(FONT, int(50*FONTSCALE))

	def get_state(self):
		return self.main.game.current_player.name

	def redraw(self):
		self.image = self.font.render(self.state, True, (255,255, 255))
		self.rect = self.image.get_rect()
		self.rect.centerx = self.centerx
		self.rect.centery = 50

class DisabledCharacter(pygame.sprite.DirtySprite):
	def __init__(self, main, character):
		pygame.sprite.DirtySprite.__init__(self)
		self.character = character
		self.main = main

		self.image = self.rect = None
		self.update()

	_layer = property(lambda self: L_CHAR_OVERLAY(self.character.grid_y), lambda self, value: None)

	def update(self):
		state = self.character.get_current_state()
		try:
			image = self.main.disabled_chartypes[state]
		except:
			image = self.character.get_current_image()[0].convert_alpha()
			image.fill((0, 0, 0, 200), special_flags=pygame.BLEND_RGBA_MULT)
			self.main.disabled_chartypes[state] = image

		self.visible = self.character.visible and self.character.player != self.main.game.current_player
		if image != self.image or self.character.rect != self.rect:
			self.dirty = 1

		self.image = image
		self.rect = self.character.rect

class Fps(pygame.sprite.DirtySprite):
	def __init__(self, clock, centerx):
		pygame.sprite.DirtySprite.__init__(self)
		self.centerx = centerx
		self.clock = clock
		self.font = pygame.font.Font(FONT, int(20*FONTSCALE))
		self.hue = 0

		self.dirty = 2

	def update(self):
		self.hue += 10

		# XXX: less flashy way to indicate that we're running smoothly
		color = get_hue_color(self.hue)
		fps = self.clock.get_fps()

		self.image = self.font.render('fps: %d' % fps, True, color)
		self.rect = self.image.get_rect()
		self.rect.centerx = self.centerx

class SpriteGrid:
	def __init__(self, grid, tile):
		self.sprites = pygame.sprite.Group()
		for i in range(len(grid)):
			self.sprites.add(Tile(tile, pygame.Rect(grid[i][0]*TILE_SIZE[0], grid[i][1]*TILE_SIZE[1], *TILE_SIZE), layer = L_SEL(grid[i][1])))

class GamePlayer:
	"""Class to create player or team in the game. One player may have many characters."""
	def __init__(self, name, chardata, main, remote):
		self.name = name
		self.main = main
		self.remote = remote
		self.all_characters = [CharacterSprite(self, character, (x, y), heading, main.map, main.res) for character, x, y, heading in chardata]

	characters = property(lambda self: [character for character in self.all_characters if character.is_alive()])
	dead_characters = property(lambda self: [character for character in self.all_characters if not character.is_alive()])

	def is_alive(self):
		return bool(self.characters)

	def get_characters_coords(self):
		coords = []
		for i in self.characters:
			coords.append(i.grid_pos)
		return coords

	def movement_points_left(self):
		points_left = 0
		for c in self.characters:
			points_left += c.mp
		return points_left

	def reset_movement_points(self):
		for c in self.all_characters:
			c.mp = c.max_mp

def roll_attack_damage(map_, attacker, defender):
	messages = []

	attacker_weapon = attacker.weapon or default_weapon
	defender_armor = defender.armor or default_armor

	defender_terrain = terrains[map_[defender.grid_pos]]

	attacker_miss_chance = attacker.per_wc_miss_chance.get(attacker_weapon.class_, 10) - attacker_weapon.magic_enchantment * 2 - attacker.class_.hit_chance
	defender_evasion_chance = defender_terrain.miss_chance + defender_armor.miss_chance + math.floor(defender.dex / 5) + defender.class_.miss_chance
	miss_chance = attacker_miss_chance + defender_evasion_chance
	hit_chance = 100 - miss_chance
	is_hit = random.randrange(100) < hit_chance

	messages.append('Hit chance is %d%%...' % hit_chance)
	if not is_hit:
		messages.append('Missed!')
		return 0, messages

	crit_chance = attacker_weapon.critical_chance + attacker.class_.crit_chance
	is_critical_hit = random.randrange(100) < crit_chance

	if is_critical_hit:
		messages.append('Critical hit!')
		messages.append('%.1fx damage!' % attacker_weapon.critical_multiplier)
	else:
		messages.append('Hit!')
	damage_multiplier = attacker_weapon.critical_multiplier if is_critical_hit else 1

	wc_damage = {'melee': attacker.str, 'ranged': attacker.dex, 'magic': attacker.int}[attacker_weapon.type]
	weapon_damage = attacker_weapon.damage.roll()

	messages.append('%s does %d+%d of %s damage on top of %d base damage.' % ((attacker_weapon.name or 'weapon').capitalize(), weapon_damage, attacker_weapon.magic_enchantment, '/'.join(attacker_weapon.damage_type), wc_damage))
	messages.append('%s negates %d of the damage on top of %d base damage reduction' % ((defender_armor.name or 'armor').capitalize(), defender_armor.damage_reduction, defender.class_.damage_reduction + math.floor(defender.con / 10) + attacker.class_.weapon_damage))

	# XXX: Magic should bypass damage reduction
	positive_damage = damage_multiplier * (weapon_damage + wc_damage + attacker_weapon.magic_enchantment + attacker.class_.weapon_damage)#+ attacker.class_(passive)_skill.damage # XXX Alexer: add passive skill damage
	negative_damage = defender.class_.damage_reduction + math.floor(defender.con / 10) + defender_armor.damage_reduction
	if not attacker_weapon.damage_type - defender_armor.enchanted_damage_reduction_type:
		messages[-1] += ' and %d of the weapon\'s %s damage.' % (defender_armor.enchanted_damage_reduction, '/'.join(defender_armor.enchanted_damage_reduction_type))
		negative_damage += defender_armor.enchanted_damage_reduction
	else:
		messages[-1] += '.'
	damage = positive_damage - negative_damage
	damage = int(math.floor(max(damage, 0)))

	messages.append('Total %d damage and %d damage reduction: Dealt %d damage.' % (positive_damage, negative_damage, damage))

	if not damage:
		messages.append('That wasn\'t very effective...')

	return damage, messages

# Following classes define the graphical elements, or Sprites.
class Button(UIComponent, pygame.sprite.DirtySprite):
	def __init__(self, x, y, width, height, text, fontsize, function, bgcolor = (139, 162, 185)):
		UIComponent.__init__(self, x, y, width, height)
		pygame.sprite.DirtySprite.__init__(self)

		self.image = pygame.Surface(self.size)

		#font = pygame.font.Font(FONT, int(fontsize*FONTSCALE))
		#image = font.render(text, True, (0, 0, 0), bgcolor)
		image = draw_pixel_text(text, SCALE)
		rect = image.get_rect()

		self.image.fill(bgcolor)
		self.image.blit(image, (self.width/2 - rect.centerx, self.height/2 - rect.centery))

		self.function = function

def get_random_teams(player_count = 2, character_count = 3):
	player_names = random.sample('Alexer Zokol brenon Ren IronBear'.split(), player_count)
	teams = []
	for name in player_names:
		characters = []
		for i in range(character_count):
			char = Character.random()
			characters.append(char)
		teams.append((name, characters))
	return teams

def serialize_team(team):
	return serialize(team, ':act:team')

def deserialize_team(team):
	return deserialize(team, ':act:team')

def serialize_spawns(players):
	return serialize(players, ':act:spawns')

def deserialize_spawns(players):
	return deserialize(players, ':act:spawns')

def serialize_path(charno, path):
	return serialize((charno, path), ':act:path')

def deserialize_path(data):
	return deserialize(data, ':act:path')

def serialize_attack(charno, path, damage, messages):
	return serialize((charno, path, damage, messages), ':act:attack')

def deserialize_attack(data):
	return deserialize(data, ':act:attack')

def compatible_protocol(version):
	return version == PROTOCOL_VERSION

def start_game(screen, mapname, teams):
	teams = [(name, None, team) for name, team in teams]
	game = XadirMain(screen, mapname = mapname)
	game.init_teams(teams, game.game.get_spawnpoints(teams))
	game.main_loop()

from screen_message import MessageWin
def show_message(screen, text, loop = False, color = (0, 255, 0)):
	win = MessageWin(screen, text, color)
	if loop:
		win.loop()
	else:
		win.draw()

def host_game(screen, port, mapname, team):
	show_message(screen, 'Waiting for connection...')
	try:
		serv = socket.socket()
		serv.bind(('0.0.0.0', port))
		serv.listen(1)
		sock, addr = serv.accept()
		serv.close()

		show_message(screen, 'Connection established')
		show_message(screen, 'Synchronizing game data...')

		proto = [False]
		other_team = [None]
		spawns = [None]
		def handler(type, data):
			if type == 'PROTOCOL':
				if compatible_protocol(data):
					proto[0] = True
			assert proto[0], 'Incompatible protocol version'
			if type == 'TEAM':
				other_team[0] = deserialize_team(data)

		conn = Messager(handler, sock)
		conn.push_message('PROTOCOL', PROTOCOL_VERSION)
		conn.push_message('MAP', mapname)
		conn.push_message('TEAM', serialize_team(team))

		while other_team[0] is None:
			asyncore.loop(count=1, timeout=0.1)
			assert asyncore.socket_map, 'Protocol error or disconnection'
	except Exception, e:
		sys.excepthook(*sys.exc_info())
		show_message(screen, 'Link failed! (%s: %s)' % (e.__class__.__name__, e.message), True, (255, 0, 0))
		return

	teams = [('Player 1', None, team), ('Player 2', conn, other_team[0])]

	game = XadirMain(screen, mapname = mapname)

	spawns = game.get_spawnpoints(teams)
	conn.push_message('SPAWNS', serialize_spawns(spawns))

	game.init_teams(teams, spawns)
	game.main_loop()

def join_game(screen, host, port, team):
	show_message(screen, 'Connecting...')
	try:
		sock = socket.socket()
		sock.connect((host, port))

		show_message(screen, 'Connection established')
		show_message(screen, 'Synchronizing game data...')

		proto = [False]
		mapname = [None]
		other_team = [None]
		spawns = [None]
		def handler(type, data):
			if type == 'PROTOCOL':
				if compatible_protocol(data):
					proto[0] = True
			assert proto[0], 'Incompatible protocol version'
			if type == 'MAP':
				mapname[0] = data
			if type == 'TEAM':
				other_team[0] = deserialize_team(data)
			if type == 'SPAWNS':
				spawns[0] = deserialize_spawns(data)

		conn = Messager(handler, sock)
		conn.push_message('PROTOCOL', PROTOCOL_VERSION)
		conn.push_message('TEAM', serialize_team(team))

		while mapname[0] is None or other_team[0] is None or spawns[0] is None:
			asyncore.loop(count=1, timeout=0.1)
			assert asyncore.socket_map, 'Protocol error or disconnection'
	except Exception, e:
		sys.excepthook(*sys.exc_info())
		show_message(screen, 'Link failed! (%s: %s)' % (e.__class__.__name__, e.message), True, (255, 0, 0))
		return

	teams = [('Player 1', conn, other_team[0]), ('Player 2', None, team)]

	game = XadirMain(screen, mapname = mapname[0])
	game.init_teams(teams, spawns[0])
	game.main_loop()

if __name__ == "__main__":
	screen = init_pygame()

	if len(sys.argv) == 1:
		start_game(screen, 'map_new.txt', get_random_teams())
	if len(sys.argv) == 2:
		host_game(screen, int(sys.argv[1]), 'map_new.txt', get_random_teams()[0][1])
	if len(sys.argv) == 3:
		join_game(screen, sys.argv[1], int(sys.argv[2]), get_random_teams()[0][1])

