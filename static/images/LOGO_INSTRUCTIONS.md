# 🎨 How to Add a Logo to Your Site

## Quick Instructions:

### 1. Prepare Your Logo

**Recommended specifications:**
- **Format:** PNG (with transparent background) or SVG
- **Size:** 200x200 pixels (or similar square/rectangular)
- **File size:** Under 100KB for fast loading
- **Colors:** Should work well on dark blue background (#667eea)

### 2. Add Logo File

Copy your logo file to:
```
flask-app/static/images/logo.png
```

Or if using SVG:
```
flask-app/static/images/logo.svg
```

### 3. Enable Logo in Template

Edit `templates/base.html` (line ~37):

**Current (logo disabled):**
```html
<!-- Logo Image (uncomment and add path to use) -->
<!-- <img src="{{ url_for('static', filename='images/logo.png') }}" alt="Logo" style="height: 40px; width: auto;"> -->

<!-- Site Title -->
<h5 class="mb-0">📥 ניהול תוכן IDF</h5>
```

**Enable logo:**
```html
<!-- Logo Image -->
<img src="{{ url_for('static', filename='images/logo.png') }}" alt="Logo" style="height: 40px; width: auto;">

<!-- Site Title (optional - can remove if logo is enough) -->
<h5 class="mb-0">ניהול תוכן IDF</h5>
```

### 4. Adjust Logo Size (Optional)

Change the `height` value to make logo bigger/smaller:

```html
<!-- Small logo -->
<img src="..." style="height: 30px; width: auto;">

<!-- Medium logo (default) -->
<img src="..." style="height: 40px; width: auto;">

<!-- Large logo -->
<img src="..." style="height: 60px; width: auto;">
```

---

## Advanced Options:

### Option A: Logo Only (No Text)

```html
<div class="d-flex align-items-center justify-content-center">
    <img src="{{ url_for('static', filename='images/logo.png') }}" 
         alt="IDF Confessions" 
         style="height: 50px; width: auto;">
</div>
```

### Option B: Logo + Text Side-by-Side

```html
<div class="d-flex align-items-center justify-content-center gap-2">
    <img src="{{ url_for('static', filename='images/logo.png') }}" 
         alt="Logo" 
         style="height: 40px; width: auto;">
    <h5 class="mb-0">ניהול תוכן IDF</h5>
</div>
```

### Option C: Logo Above Text

```html
<div class="d-flex flex-column align-items-center justify-content-center gap-2">
    <img src="{{ url_for('static', filename='images/logo.png') }}" 
         alt="Logo" 
         style="height: 50px; width: auto;">
    <h6 class="mb-0">ניהול תוכן</h6>
</div>
```

### Option D: Circular Logo with Border

```html
<img src="{{ url_for('static', filename='images/logo.png') }}" 
     alt="Logo" 
     style="height: 50px; width: 50px; border-radius: 50%; border: 2px solid white; padding: 5px; background: white;">
```

---

## Using SVG Logo:

SVG files are scalable and look crisp at any size:

```html
<img src="{{ url_for('static', filename='images/logo.svg') }}" 
     alt="Logo" 
     style="height: 40px; width: auto;">
```

**Benefits of SVG:**
- Perfect clarity at any size
- Small file size
- Can change colors with CSS

---

## File Structure:

```
flask-app/
├── static/
│   ├── css/
│   ├── js/
│   └── images/           ← Create this directory
│       ├── logo.png      ← Put your logo here
│       ├── logo.svg      ← Or SVG version
│       └── favicon.ico   ← Optional: browser tab icon
├── templates/
│   └── base.html         ← Edit this file to enable logo
└── app.py
```

---

## Adding Favicon (Browser Tab Icon):

1. Create/get a `favicon.ico` file (16x16 or 32x32 pixels)
2. Place in `static/images/favicon.ico`
3. Add to `base.html` in the `<head>` section:

```html
<link rel="icon" type="image/x-icon" href="{{ url_for('static', filename='images/favicon.ico') }}">
```

---

## Testing:

After adding logo:

1. Restart Flask app:
   ```bash
   python app.py
   ```

2. Open browser and force refresh:
   ```
   Ctrl+Shift+R (or Cmd+Shift+R on Mac)
   ```

3. Check sidebar - logo should appear!

---

## Troubleshooting:

### Logo not showing?

**Check file path:**
```bash
# Should exist:
ls static/images/logo.png
```

**Check template:**
- Make sure you removed `<!--` and `-->` comments
- Make sure filename matches exactly (case-sensitive!)

**Clear browser cache:**
```
Ctrl+Shift+R
```

### Logo too big/small?

Adjust the `height` value in the `<img>` tag:
- Too big → Use smaller number (e.g., `30px`)
- Too small → Use larger number (e.g., `60px`)

### Logo looks blurry?

- Use higher resolution image (at least 200x200)
- Or switch to SVG format (always crisp)

---

## Example Logo URLs:

If you want to test with an online logo before adding your own:

```html
<!-- Test with IDF logo from online -->
<img src="https://upload.wikimedia.org/wikipedia/commons/thumb/a/a0/Israel_Defense_Forces_Logo.svg/200px-Israel_Defense_Forces_Logo.svg.png" 
     alt="IDF Logo" 
     style="height: 40px; width: auto;">
```

**Note:** For production, always use local files (in `static/images/`) for reliability.

---

## Summary:

1. ✅ Create `static/images/` directory
2. ✅ Add your `logo.png` file there
3. ✅ Edit `templates/base.html` line ~37
4. ✅ Uncomment the `<img>` tag
5. ✅ Adjust size as needed
6. ✅ Restart app and refresh browser

**Your logo will appear in the sidebar!** 🎉
