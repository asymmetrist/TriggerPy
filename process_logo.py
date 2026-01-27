"""
Script to crop and resize the ArcTrigger logo to 1400x60 pixels for the banner.
Usage: python process_logo.py [image_path]
"""
from PIL import Image
import os
import glob
import sys

# Get image path from command line or search for it
if len(sys.argv) > 1:
    source_image = sys.argv[1]
else:
    # Try to find the source image in common locations
    possible_paths = [
        r"C:\Users\himitrades\.cursor\projects\c-Users-himitrades-TriggerPy-1\assets\*.png",
        "*.png",
        "logo*.png",
        "arctrigger*.png",
    ]
    
    source_image = None
    for pattern in possible_paths:
        matches = glob.glob(pattern)
        if matches:
            # Use the first PNG found, prefer larger files (likely the source)
            matches.sort(key=lambda x: os.path.getsize(x) if os.path.exists(x) else 0, reverse=True)
            source_image = matches[0]
            break

# Output path
output_image = "banner_logo.png"

# Target dimensions
target_width = 1400
target_height = 60

if not source_image:
    print("=" * 60)
    print("No source image found automatically.")
    print("=" * 60)
    print("\nTo process your logo image, please:")
    print("1. Place your ARCTRIGGER logo image (PNG format) in this directory")
    print("2. Run: python process_logo.py <path_to_your_image.png>")
    print("\nOr modify the script to include your image path.")
    print("\nThe script will:")
    print("  - Crop the logo area from the image")
    print("  - Resize it to 1400x60 pixels")
    print("  - Save it as 'banner_logo.png' for use in the application")
    exit(1)

try:
    # Open the source image
    img = Image.open(source_image)
    print(f"Found source image: {source_image}")
    print(f"Original image size: {img.size}")
    
    # Based on the image description, crop the logo area
    # Estimated bounding box: x:350-1570, y:400-650 (width:1220, height:250)
    # We'll crop a bit wider to include more of the logo area
    # If the image is 1920x1080, use these coordinates
    img_width, img_height = img.size
    
    # Calculate crop coordinates based on actual image size
    # Original was 1920x1080, so we scale proportionally
    scale_x = img_width / 1920.0
    scale_y = img_height / 1080.0
    
    left = int(300 * scale_x)
    top = int(380 * scale_y)
    right = int(1620 * scale_x)
    bottom = int(680 * scale_y)
    
    # Ensure coordinates are within image bounds
    left = max(0, min(left, img_width))
    top = max(0, min(top, img_height))
    right = max(left, min(right, img_width))
    bottom = max(top, min(bottom, img_height))
    
    # Crop the logo area
    cropped = img.crop((left, top, right, bottom))
    print(f"Cropped image size: {cropped.size}")
    
    # Resize to target dimensions (1400x60)
    # Using LANCZOS for high-quality resizing
    resized = cropped.resize((target_width, target_height), Image.Resampling.LANCZOS)
    print(f"Resized image size: {resized.size}")
    
    # Save the processed logo
    resized.save(output_image, "PNG", optimize=True)
    print(f"✓ Logo saved to: {output_image}")
    print(f"  Ready to use in the application!")
    
except FileNotFoundError:
    print(f"Source image not found: {source_image}")
    print("Please check the image path.")
except Exception as e:
    print(f"Error processing image: {e}")
    import traceback
    traceback.print_exc()
