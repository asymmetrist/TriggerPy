# ArcTrigger Logo Setup Guide

## Overview
The ArcTrigger banner now supports displaying a custom logo image instead of text.

## Banner Dimensions
- **Width:** 1400 pixels
- **Height:** 60 pixels

## Processing Your Logo

### Step 1: Prepare Your Image
1. Make sure you have your ARCTRIGGER logo image (PNG format recommended)
2. The original image should be at least 1400x60 pixels, or larger (will be resized)

### Step 2: Process the Image
Run the processing script:

```bash
python process_logo.py <path_to_your_image.png>
```

For example:
```bash
python process_logo.py "C:\path\to\your\arctrigger_logo.png"
```

The script will:
- Automatically crop the logo area from your image
- Resize it to exactly 1400x60 pixels
- Save it as `banner_logo.png` in the project root

### Step 3: Verify
After processing, you should see `banner_logo.png` in the project root directory.

### Step 4: Run the Application
The application will automatically use `banner_logo.png` if it exists. If the file is not found, it will fall back to the text-based logo.

## Image Processing Details

The script uses the following crop coordinates (scaled to your image size):
- **Left:** 300px (scaled)
- **Top:** 380px (scaled)  
- **Right:** 1620px (scaled)
- **Bottom:** 680px (scaled)

These coordinates are based on a 1920x1080 source image. The script automatically scales them for different image sizes.

## Troubleshooting

- **Image not found:** Make sure the image path is correct and the file exists
- **Poor quality:** Use a high-resolution source image (at least 1400px wide)
- **Logo not showing:** Check that `banner_logo.png` exists in the project root
- **Fallback to text:** If the image can't be loaded, the app will use the text logo

## For PyInstaller Builds

If you're building an executable with PyInstaller, make sure to include `banner_logo.png` in your `.spec` file:

```python
datas=[('banner_logo.png', '.')]
```
