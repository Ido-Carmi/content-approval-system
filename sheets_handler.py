import gspread
from google.oauth2.service_account import Credentials
from typing import List, Dict
import pandas as pd
from datetime import datetime

class SheetsHandler:
    def __init__(self, sheet_id: str, credentials_file: str = 'credentials.json'):
        """
        Initialize Google Sheets handler
        
        Args:
            sheet_id: The Google Sheet ID from the URL
            credentials_file: Path to the service account credentials JSON file
        """
        self.sheet_id = sheet_id
        self.credentials_file = credentials_file
        self.client = None
        self.sheet = None
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with Google Sheets API"""
        try:
            # Define the scope
            scope = [
                'https://www.googleapis.com/auth/spreadsheets.readonly',
                'https://www.googleapis.com/auth/drive.readonly'
            ]
            
            # Try to load from Streamlit secrets first (for cloud deployment)
            try:
                import streamlit as st
                if hasattr(st, 'secrets') and 'google_credentials' in st.secrets:
                    creds_dict = dict(st.secrets['google_credentials'])
                    creds = Credentials.from_service_account_info(
                        creds_dict,
                        scopes=scope
                    )
                else:
                    raise Exception("No Streamlit secrets found")
            except:
                # Fallback to file for local development
                creds = Credentials.from_service_account_file(
                    self.credentials_file,
                    scopes=scope
                )
            
            # Create client
            self.client = gspread.authorize(creds)
            
            # Open the spreadsheet
            self.sheet = self.client.open_by_key(self.sheet_id).sheet1
            
        except Exception as e:
            raise Exception(f"Failed to authenticate with Google Sheets: {str(e)}")
    
    def fetch_all_data(self) -> pd.DataFrame:
        """Fetch all data from the sheet"""
        try:
            # Get all records as a list of dictionaries
            records = self.sheet.get_all_records()
            
            # Convert to DataFrame
            df = pd.DataFrame(records)
            
            return df
        except Exception as e:
            raise Exception(f"Failed to fetch data from Google Sheets: {str(e)}")
    
    def fetch_new_entries(self, last_processed_Timestamp: str = None) -> List[Dict]:
        """
        Fetch new entries from the sheet
        
        Args:
            last_processed_Timestamp: The Timestamp of the last processed entry
            
        Returns:
            List of new entries with 'Timestamp' and 'text' keys
        """
        try:
            df = self.fetch_all_data()
            
            # Clean column names - remove any hidden characters
            df.columns = [col.strip() for col in df.columns]
            
            print("CLEANED COLUMNS:", df.columns.tolist())
            print("Column 0:", repr(df.columns[0]))
            print("Column 1:", repr(df.columns[1]))
            
            # Use exact column names - reference by index to avoid encoding issues
            Timestamp_col = df.columns[0]  # First column (Timestamp)
            confession_col = df.columns[1]  # Second column (הוידוי שלך:)
            
            print(f"Using Timestamp_col: {repr(Timestamp_col)}")
            print(f"Using confession_col: {repr(confession_col)}")
            
            # Rename to standard names
            df = df.rename(columns={Timestamp_col: 'timestamp', confession_col: 'confession'})
            
            # Filter out empty entries
            # Filter out empty entries
            df = df[df['confession'].notna() & (df['confession'] != '')]

            # Convert timestamp to string
            df['timestamp'] = df['timestamp'].astype(str)

            # Filter by last processed timestamp if provided
            if last_processed_Timestamp:
                try:
                    print(f"=" * 60)
                    print(f"DATE FILTERING DEBUG")
                    print(f"=" * 60)
                    print(f"Input last_processed_Timestamp = '{last_processed_Timestamp}'")
                    print(f"Total rows before filtering: {len(df)}")
                    
                    # Show first 5 raw timestamps
                    print(f"\nFirst 5 RAW timestamps from sheet:")
                    for i, ts in enumerate(df['timestamp'].head(5)):
                        print(f"  [{i}] '{ts}' (type: {type(ts)})")
                    
                    # Parse timestamps
                    df['timestamp_dt'] = pd.to_datetime(df['timestamp'], errors='coerce', dayfirst=True)
                    last_dt = pd.to_datetime(last_processed_Timestamp, errors='coerce', dayfirst=True)
                    
                    print(f"\nParsed filter date: {last_dt}")
                    print(f"Filter date type: {type(last_dt)}")
                    
                    print(f"\nFirst 5 PARSED timestamps:")
                    for i, ts in enumerate(df['timestamp_dt'].head(5)):
                        print(f"  [{i}] {ts}")
                    
                    # Show comparison
                    print(f"\nComparison (should keep rows where timestamp > {last_dt}):")
                    for i in range(min(5, len(df))):
                        ts = df['timestamp_dt'].iloc[i]
                        keep = ts > last_dt if pd.notna(ts) and pd.notna(last_dt) else False
                        print(f"  [{i}] {ts} > {last_dt} = {keep}")
                    
                    if pd.notna(last_dt):
                        before_count = len(df)
                        df = df[df['timestamp_dt'] > last_dt]
                        after_count = len(df)
                        print(f"\nFILTERING RESULT:")
                        print(f"  Before: {before_count} entries")
                        print(f"  After:  {after_count} entries")
                        print(f"  Removed: {before_count - after_count} entries")
                    else:
                        print(f"\nWARNING: last_dt is NaT (not a valid date), skipping filter")
                    
                    df = df.drop('timestamp_dt', axis=1)
                    print(f"=" * 60)
                except Exception as e:
                    print(f"ERROR in date filtering: {e}")
                    import traceback
                    traceback.print_exc()

            # Convert to list of dictionaries
            entries = []
            for _, row in df.iterrows():
                entries.append({
                    'timestamp': str(row['timestamp']),
                    'text': str(row['confession'])
                })
            
            return entries
            
        except Exception as e:
            raise Exception(f"Failed to fetch new entries: {str(e)}") 
        
          
    def get_latest_Timestamp(self) -> str:
        """Get the Timestamp of the most recent entry"""
        try:
            df = self.fetch_all_data()
            
            # Find Timestamp column
            Timestamp_col = None
            for col in df.columns:
                if col.lower() == 'Timestamp':
                    Timestamp_col = col
                    break
            
            if Timestamp_col and not df.empty:
                df['Timestamp_dt'] = pd.to_datetime(df[Timestamp_col], errors='coerce')
                latest = df['Timestamp_dt'].max()
                return str(latest)
            
            return None
        except Exception as e:
            raise Exception(f"Failed to get latest Timestamp: {str(e)}")
    
    def test_connection(self) -> bool:
        """Test if the connection to Google Sheets is working"""
        try:
            # Try to fetch the sheet title
            title = self.sheet.title
            return True
        except Exception as e:
            return False