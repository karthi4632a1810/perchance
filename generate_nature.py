from PIL import Image, ImageDraw

# Create a blank image with a light blue sky
width, height = 800, 600
img = Image.new('RGB', (width, height), color=(135, 206, 235))
draw = ImageDraw.Draw(img)

# Draw grass (green rectangle at the bottom)
draw.rectangle([0, 400, 800, 600], fill=(34, 139, 34))

# Draw a sun (yellow circle)
draw.ellipse([600, 50, 700, 150], fill=(255, 223, 0))

# Draw a simple tree
# Trunk
draw.rectangle([380, 300, 420, 450], fill=(139, 69, 19))
# Leaves
draw.ellipse([300, 200, 500, 400], fill=(34, 139, 34))

img.save('nature_image.png')
print('Image saved as nature_image.png')
