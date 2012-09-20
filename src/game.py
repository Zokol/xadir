import os, sys, time
import pygame
from pygame.locals import *
from resources import *
from config import *

if not pygame.font:
	print "Warning: Fonts not enabled"
if not pygame.mixer:
	print "Warning: Audio not enabled"

class GameMain:
	"""Main class for init and creation of game."""
	def __init__(self, width=640, height=480):
		pygame.init()
		self.width = width
		self.height = height
		self.screen = pygame.display.set_mode((self.width, self.height))

	def loop(self):
		self.load_sprites()
		while 1:
			self.map_sprites.draw(self.screen)
			pygame.display.flip()
			for event in pygame.event.get():
				if event.type == pygame.QUIT:
					sys.exit()
			time.sleep(0.05)

	def load_sprites(self):
		"""Load the sprites that we need"""
		self.map = Map([[0,0,0,0,0],[0,1,1,1,0],[0,0,1,0,0],[0,0,1,1,0]], 5, 4)
		self.map_sprites = self.map.get_sprites()

class Map:
	"""Class for initialization of the map layer"""
	def __init__(self, map, height, width):
		self.sprite = []
		self.width = width
		self.height = height
		self.map = map
		self.map_sprites = pygame.sprite.Group()
		for x in range(self.width):
			for y in range(self.height):
				if map[x][y]:
					print "Water at: (" + str(x) + "," + str(y) + ")"
					self.map_sprites.add(Water(pygame.Rect(y*SIZE[1], x*SIZE[0], *SIZE)))
				else:
					print "Land at: (" + str(x) + "," + str(y) + ")"
					self.map_sprites.add(Land(pygame.Rect(y*SIZE[1], x*SIZE[0], *SIZE)))

	def get_sprites(self):
		return self.map_sprites

class Land(pygame.sprite.Sprite):
	def __init__(self, rect=None):
		pygame.sprite.Sprite.__init__(self)
		self.image = lands[1][1]
		self.rect = self.image.get_rect()
		if rect != None:
			self.rect = rect

class Water(pygame.sprite.Sprite):
	def __init__(self, rect=None):
		pygame.sprite.Sprite.__init__(self)
		self.image = waters[1][1]
		self.rect = self.image.get_rect()
		if rect != None:
			self.rect = rect

SIZE = (16, 16)
SIZE2 = (3 * SIZE[0], 3 * SIZE[1])

if __name__ == "__main__":
	game = GameMain()

	tiles = load_tiles('placeholder_tilemap.png', SIZE2)
	waters = parse_tiles(tiles[0][0], SIZE)
	lands = parse_tiles(tiles[0][1], SIZE)

	game.loop()

