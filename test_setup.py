#!/usr/bin/env python3
"""
Test script to verify your setup before running the main application
Run this script to check if all credentials and connections are working
"""

import json
from pathlib import Path
import sys

def test_google_sheets():
    """Test Google Sheets connection"""
    print("\n" + "="*50)
    print("Testing Google Sheets Connection")
    print("="*50)
    
    try:
        from sheets_handler import SheetsHandler
        
        # Load config
        if not Path("config.json").exists():
            print("❌ config.json not found. Please configure the app first.")
            return False
        
        with open("config.json", 'r') as f:
            config = json.load(f)
        
        sheet_id = config.get('google_sheet_id')
        creds_file = config.get('google_credentials_file', 'credentials.json')
        
        if not sheet_id:
            print("❌ Google Sheet ID not configured")
            return False
        
        if not Path(creds_file).exists():
            print(f"❌ Credentials file '{creds_file}' not found")
            return False
        
        print(f"✓ Config file found")
        print(f"✓ Credentials file found")
        print(f"✓ Sheet ID: {sheet_id[:20]}...")
        
        # Test connection
        print("\nAttempting to connect...")
        handler = SheetsHandler(sheet_id, creds_file)
        
        if handler.test_connection():
            print("✅ Google Sheets connection successful!")
            
            # Try to fetch data
            print("\nFetching sample data...")
            data = handler.fetch_all_data()
            print(f"✓ Found {len(data)} rows in sheet")
            print(f"✓ Columns: {list(data.columns)}")
            
            return True
        else:
            print("❌ Connection test failed")
            return False
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

def test_facebook():
    """Test Facebook connection"""
    print("\n" + "="*50)
    print("Testing Facebook Connection")
    print("="*50)
    
    try:
        from facebook_handler import FacebookHandler
        
        # Load config
        if not Path("config.json").exists():
            print("❌ config.json not found. Please configure the app first.")
            return False
        
        with open("config.json", 'r') as f:
            config = json.load(f)
        
        page_id = config.get('facebook_page_id')
        access_token = config.get('facebook_access_token')
        
        if not page_id or not access_token:
            print("❌ Facebook credentials not configured")
            return False
        
        print(f"✓ Page ID: {page_id}")
        print(f"✓ Access token: {access_token[:20]}...")
        
        # Test connection
        print("\nAttempting to connect...")
        handler = FacebookHandler(page_id, access_token)
        
        if handler.test_connection():
            print("✅ Facebook connection successful!")
            
            # Get page info
            info = handler.get_page_info()
            if info:
                print(f"✓ Page Name: {info.get('name')}")
                print(f"✓ Page ID: {info.get('id')}")
                if 'category' in info:
                    print(f"✓ Category: {info.get('category')}")
            
            return True
        else:
            print("❌ Connection test failed")
            print("   Check your access token and page ID")
            return False
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

def test_database():
    """Test database creation"""
    print("\n" + "="*50)
    print("Testing Database")
    print("="*50)
    
    try:
        from database import Database
        
        db = Database()
        print("✅ Database initialized successfully")
        
        stats = db.get_statistics()
        print(f"✓ Pending entries: {stats['pending']}")
        print(f"✓ Approved entries: {stats['approved']}")
        print(f"✓ Scheduled posts: {stats['scheduled']}")
        print(f"✓ Published posts: {stats['published']}")
        
        current_number = db.get_current_post_number()
        print(f"✓ Current post number: #{current_number}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

def test_dependencies():
    """Test if all required packages are installed"""
    print("\n" + "="*50)
    print("Testing Dependencies")
    print("="*50)
    
    required_packages = [
        ('streamlit', 'Streamlit'),
        ('pandas', 'Pandas'),
        ('gspread', 'GSpread'),
        ('google.auth', 'Google Auth'),
        ('requests', 'Requests'),
        ('pytz', 'Pytz'),
        ('schedule', 'Schedule'),
    ]
    
    all_ok = True
    for package, name in required_packages:
        try:
            __import__(package)
            print(f"✓ {name}")
        except ImportError:
            print(f"❌ {name} - not installed")
            all_ok = False
    
    if all_ok:
        print("\n✅ All dependencies installed")
        return True
    else:
        print("\n❌ Some dependencies missing")
        print("Run: pip install -r requirements.txt")
        return False

def main():
    """Run all tests"""
    print("\n" + "="*70)
    print(" Content Approval System - Setup Verification ".center(70, "="))
    print("="*70)
    
    results = {}
    
    # Test dependencies first
    results['dependencies'] = test_dependencies()
    
    # Test database
    results['database'] = test_database()
    
    # Test Google Sheets
    results['google_sheets'] = test_google_sheets()
    
    # Test Facebook
    results['facebook'] = test_facebook()
    
    # Print summary
    print("\n" + "="*70)
    print(" TEST SUMMARY ".center(70, "="))
    print("="*70)
    
    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{test_name.replace('_', ' ').title()}: {status}")
    
    all_passed = all(results.values())
    
    print("\n" + "="*70)
    if all_passed:
        print("🎉 All tests passed! You're ready to run the application.")
        print("\nNext steps:")
        print("1. Run the app: streamlit run app.py")
        print("2. Go to Settings and sync with Google Sheets")
        print("3. Review and approve entries")
        print("\nFor automatic sync and publishing, also run:")
        print("   python background_jobs.py")
    else:
        print("⚠️  Some tests failed. Please check the errors above.")
        print("\nCommon solutions:")
        print("1. Install dependencies: pip install -r requirements.txt")
        print("2. Create config.json: Run the app and go to Settings")
        print("3. Add credentials.json from Google Cloud Console")
        print("4. Verify Facebook access token is valid")
    
    print("="*70 + "\n")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
