"""
AI Comment Filter using Claude API
Analyzes comments for policy violations
"""

import os
import json
import anthropic

class CommentFilter:
    def __init__(self, api_key=None):
        """
        Initialize the comment filter
        api_key: Anthropic API key (if None, will use ANTHROPIC_API_KEY env variable)
        """
        self.api_key = api_key or os.environ.get('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("Anthropic API key not provided")
        
        self.client = anthropic.Anthropic(api_key=self.api_key)
    
    def analyze_comment(self, comment_text, context=""):
        """
        Analyze a comment for policy violations
        
        Returns dict:
        {
            'should_hide': bool,
            'reason': str,
            'category': str,  # 'political', 'hate', 'clean'
            'confidence': float  # 0-1
        }
        """
        
        prompt = f"""Analyze this Facebook comment for policy violations.

Discussion Rules:
1. No overly political content
2. No slurs or hate speech

Context (optional): {context}

Comment to analyze:
"{comment_text}"

Respond ONLY with valid JSON in this exact format:
{{
    "should_hide": true/false,
    "reason": "brief explanation",
    "category": "political" or "hate" or "clean",
    "confidence": 0.0-1.0
}}

Be strict but fair. Hide comments that clearly violate rules."""

        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            # Extract JSON from response
            response_text = message.content[0].text.strip()
            
            # Remove markdown code blocks if present
            if response_text.startswith('```'):
                response_text = response_text.split('```')[1]
                if response_text.startswith('json'):
                    response_text = response_text[4:]
                response_text = response_text.strip()
            
            result = json.loads(response_text)
            
            # Validate result has required keys
            required_keys = ['should_hide', 'reason', 'category', 'confidence']
            if not all(key in result for key in required_keys):
                raise ValueError("Missing required keys in AI response")
            
            return result
            
        except Exception as e:
            print(f"Error analyzing comment: {e}")
            # Default to not hiding if AI fails
            return {
                'should_hide': False,
                'reason': f'AI analysis failed: {str(e)}',
                'category': 'error',
                'confidence': 0.0
            }
    
    def batch_analyze(self, comments, context=""):
        """
        Analyze multiple comments
        
        comments: List of dicts with 'id' and 'message' keys
        Returns: Dict mapping comment_id to analysis result
        """
        results = {}
        
        for comment in comments:
            comment_id = comment.get('id')
            comment_text = comment.get('message', '')
            
            if not comment_text:
                results[comment_id] = {
                    'should_hide': False,
                    'reason': 'Empty comment',
                    'category': 'clean',
                    'confidence': 1.0
                }
                continue
            
            # Add small delay to avoid rate limits
            import time
            time.sleep(0.5)
            
            analysis = self.analyze_comment(comment_text, context)
            results[comment_id] = analysis
        
        return results
