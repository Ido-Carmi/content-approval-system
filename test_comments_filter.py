#!/usr/bin/env python3
"""
Test Comments Filter Feature
Run this to verify everything is working
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_database():
    """Test that new tables exist"""
    print("\n" + "="*60)
    print("TEST 1: Database Tables")
    print("="*60)
    
    try:
        from database import Database
        db = Database()
        
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Check for new tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        required_tables = ['hidden_comments', 'comment_queue', 'post_tracking']
        
        for table in required_tables:
            if table in tables:
                print(f"  ✅ Table '{table}' exists")
            else:
                print(f"  ❌ Table '{table}' MISSING")
                return False
        
        conn.close()
        print("\n✅ All database tables created successfully")
        return True
        
    except Exception as e:
        print(f"\n❌ Database test failed: {e}")
        return False

def test_facebook_handler():
    """Test Facebook handler import"""
    print("\n" + "="*60)
    print("TEST 2: Facebook Comments Handler")
    print("="*60)
    
    try:
        from facebook_comments_handler import FacebookCommentsHandler
        print("  ✅ Module imports successfully")
        
        # Test initialization (will fail without API key, but that's ok)
        handler = FacebookCommentsHandler(
            access_token="test_token",
            page_id="test_page"
        )
        print("  ✅ Handler initializes")
        
        print("\n✅ Facebook handler working")
        return True
        
    except Exception as e:
        print(f"\n❌ Facebook handler test failed: {e}")
        return False

def test_ai_filter():
    """Test AI filter import"""
    print("\n" + "="*60)
    print("TEST 3: AI Comment Filter")
    print("="*60)
    
    try:
        from ai_comment_filter import CommentFilter
        print("  ✅ Module imports successfully")
        
        # Check if openai is installed
        try:
            import openai
            print("  ✅ OpenAI package installed")
        except ImportError:
            print("  ⚠️  OpenAI package NOT installed")
            print("     Run: pip install openai --break-system-packages")
            return False
        
        print("\n✅ AI filter ready")
        return True
        
    except Exception as e:
        print(f"\n❌ AI filter test failed: {e}")
        return False

def test_scanner():
    """Test comments scanner"""
    print("\n" + "="*60)
    print("TEST 4: Comments Scanner")
    print("="*60)
    
    try:
        from comments_scanner import CommentsScanner, create_hourly_job
        print("  ✅ Module imports successfully")
        
        print("\n✅ Scanner ready")
        return True
        
    except Exception as e:
        print(f"\n❌ Scanner test failed: {e}")
        return False

def test_config():
    """Test configuration loading"""
    print("\n" + "="*60)
    print("TEST 5: Configuration")
    print("="*60)
    
    try:
        import json
        from pathlib import Path
        
        config_file = Path("config.json")
        if not config_file.exists():
            print("  ⚠️  config.json not found (will be created on first run)")
            return True
        
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        print("  ✅ Config file exists")
        
        # Check for comments filter settings
        if config.get('openai_api_key'):
            print("  ✅ OpenAI API key configured")
        else:
            print("  ⚠️  OpenAI API key not set (configure in Settings)")
        
        if config.get('comments_filter_enabled'):
            print("  ✅ Comments filter enabled")
        else:
            print("  ⚠️  Comments filter disabled (enable in Settings)")
        
        print("\n✅ Configuration OK")
        return True
        
    except Exception as e:
        print(f"\n❌ Config test failed: {e}")
        return False

def test_routes():
    """Test Flask routes"""
    print("\n" + "="*60)
    print("TEST 6: Flask Routes")
    print("="*60)
    
    try:
        from app import app
        
        with app.test_client() as client:
            # Test hidden comments page
            response = client.get('/hidden-comments')
            if response.status_code == 200:
                print("  ✅ /hidden-comments route works")
            else:
                print(f"  ❌ /hidden-comments returned {response.status_code}")
                return False
        
        print("\n✅ Routes working")
        return True
        
    except Exception as e:
        print(f"\n❌ Routes test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("\n" + "="*60)
    print("🧪 COMMENTS FILTER FEATURE - TEST SUITE")
    print("="*60)
    
    tests = [
        ("Database Tables", test_database),
        ("Facebook Handler", test_facebook_handler),
        ("AI Filter", test_ai_filter),
        ("Comments Scanner", test_scanner),
        ("Configuration", test_config),
        ("Flask Routes", test_routes),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n❌ Test '{name}' crashed: {e}")
            results.append((name, False))
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
    
    print("\n" + "="*60)
    print(f"Result: {passed}/{total} tests passed")
    print("="*60)
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED! Feature is ready to use.")
        print("\nNext steps:")
        print("  1. Configure OpenAI API key in Settings")
        print("  2. Enable Comments Filter in Settings")
        print("  3. Visit /hidden-comments to see admin page")
        return 0
    else:
        print("\n⚠️  SOME TESTS FAILED. Check errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
