"""
AI Comment Filter
Uses OpenAI GPT-4o-mini to detect political content and hate speech in comments
Batch processing for cost efficiency
"""

import os
from typing import List, Dict
import json

class CommentFilter:
    def __init__(self, api_key: str, db=None):
        """Initialize AI Comment Filter with OpenAI API key"""
        self.api_key = api_key
        self.model = "gpt-4o-mini"  # Cheapest and effective
        self.db = db  # Database for feedback examples
        
        # Try to import openai
        try:
            import openai
            self.client = openai.OpenAI(api_key=api_key)
        except ImportError:
            print("⚠️  OpenAI package not installed. Run: pip install openai")
            self.client = None
    
    def _build_system_prompt(self) -> str:
        """Build system prompt with few-shot learning from curated examples"""
        
        base_prompt = """You are a content moderator for a Facebook page.
Your job is to identify comments that violate these rules:

1. **Too Political**: Comments discussing political parties, politicians, elections, or government policies
2. **Hate Speech**: Comments containing slurs, personal attacks, discrimination, or hateful language
3. **Spam**: Commercial advertisements, repeated messages, off-topic promotions, or bot-like content

Context: This is an IDF (Israel Defense Forces) confessions page where soldiers share experiences.
Some military/army discussion is okay, but partisan politics, hate speech, and spam are not."""

        # Add few-shot examples from permanent curated examples
        if self.db:
            try:
                examples = self.db.get_ai_examples_for_learning()
                
                has_examples = any(examples.values())
                
                if has_examples:
                    base_prompt += "\n\n## 🎓 Learn from These Curated Examples:\n"
                
                # FALSE POSITIVES - What NOT to flag
                fp_political = examples.get('false_positive_political', [])
                fp_hate = examples.get('false_positive_hate', [])
                fp_spam = examples.get('false_positive_spam', [])
                
                if fp_political or fp_hate or fp_spam:
                    base_prompt += "\n**❌ DON'T Flag These (Acceptable Comments):**\n"
                    
                    for ex in fp_political:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ❌ NOT political - this is acceptable\n"
                    
                    for ex in fp_hate:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ❌ NOT hate speech - this is acceptable\n"
                    
                    for ex in fp_spam:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ❌ NOT spam - this is acceptable\n"
                
                # CORRECT PREDICTIONS - Reinforce good behavior
                correct_political = examples.get('correct_political', [])
                correct_hate = examples.get('correct_hate', [])
                correct_spam = examples.get('correct_spam', [])
                
                if correct_political or correct_hate or correct_spam:
                    base_prompt += "\n**✓ Correctly Flagged (Keep Doing This):**\n"
                    
                    for ex in correct_political:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ Correctly identified as POLITICAL\n"
                    
                    for ex in correct_hate:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ Correctly identified as HATE SPEECH\n"
                    
                    for ex in correct_spam:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ Correctly identified as SPAM\n"
                
                # MISSED VIOLATIONS - What TO flag
                missed_political = examples.get('missed_political', [])
                missed_hate = examples.get('missed_hate', [])
                missed_spam = examples.get('missed_spam', [])
                
                if missed_political or missed_hate or missed_spam:
                    base_prompt += "\n**✅ DO Flag These (Violations You Missed Before):**\n"
                    
                    for ex in missed_political:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ This IS political - flag it\n"
                    
                    for ex in missed_hate:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ This IS hate speech - flag it\n"
                    
                    for ex in missed_spam:
                        base_prompt += f"\n- \"{ex['comment_text']}\""
                        base_prompt += f"\n  ✓ This IS spam - flag it\n"
                
                total_examples = sum(len(v) for v in examples.values())
                if total_examples > 0:
                    print(f"   [AI] Using {total_examples} curated examples across 9 categories")
                
            except Exception as e:
                print(f"   [AI] Warning: Could not load examples: {e}")
        
        base_prompt += """

For each comment, determine:
- Should it be hidden? (yes/no)
- What rule does it violate? (political/hate/spam/none)
- Brief explanation (1 sentence)

Respond ONLY with valid JSON array, no markdown formatting:
[
  {"id": "comment_id", "hide": true/false, "reason": "political"/"hate"/"spam"/null, "explanation": "brief reason"},
  ...
]"""
        
        return base_prompt
    
    def filter_comments_batch(self, comments: List[Dict], batch_size: int = 50) -> List[Dict]:
        """
        Filter comments in batches using AI
        
        Args:
            comments: List of comment dictionaries with 'comment_id' and 'comment_text'
            batch_size: Number of comments per API call (max 50 for efficiency)
        
        Returns:
            List of dictionaries with filter results:
            [
                {
                    'comment_id': '123',
                    'should_hide': True,
                    'reason': 'political',  # or 'hate' or None
                    'explanation': 'AI reasoning...'
                },
                ...
            ]
        """
        if not self.client:
            print("❌ OpenAI client not initialized")
            return []
        
        all_results = []
        
        # Process in batches
        for i in range(0, len(comments), batch_size):
            batch = comments[i:i + batch_size]
            
            try:
                results = self._filter_batch(batch)
                all_results.extend(results)
                print(f"✅ Filtered batch {i//batch_size + 1}: {len(results)} comments")
            except Exception as e:
                print(f"❌ Error filtering batch {i//batch_size + 1}: {e}")
                
                # On error, queue these comments for retry
                for comment in batch:
                    all_results.append({
                        'comment_id': comment['comment_id'],
                        'should_hide': False,
                        'reason': None,
                        'explanation': f'Error: {str(e)}',
                        'needs_retry': True
                    })
        
        return all_results
    
    def _filter_batch(self, comments: List[Dict]) -> List[Dict]:
        """
        Send one batch to OpenAI API
        
        Returns:
            List of filter results
        """
        # Prepare comments for AI
        comments_text = []
        for idx, comment in enumerate(comments):
            comments_text.append({
                'id': comment['comment_id'],
                'text': comment['comment_text']
            })
        
        # Build smart system prompt with few-shot examples
        system_prompt = self._build_system_prompt()

        user_prompt = f"""Review these comments and identify violations:

{json.dumps(comments_text, ensure_ascii=False, indent=2)}

Return JSON array with moderation results."""

        # Call OpenAI API
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,  # Low temperature for consistent moderation
                max_tokens=4000,  # Increased for larger batches
                response_format={"type": "json_object"}  # Force JSON response
            )
            
            # Parse response
            content = response.choices[0].message.content
            
            # Try to parse JSON
            try:
                # If wrapped in ```json, extract it
                if '```json' in content:
                    content = content.split('```json')[1].split('```')[0].strip()
                elif '```' in content:
                    content = content.split('```')[1].split('```')[0].strip()
                
                results_data = json.loads(content)
                
                print(f"   [AI] Response type: {type(results_data)}")
                print(f"   [AI] Response keys: {results_data.keys() if isinstance(results_data, dict) else 'N/A'}")
                
                # Handle both array and object with array
                if isinstance(results_data, dict):
                    # Try different possible keys
                    if 'results' in results_data:
                        results_data = results_data['results']
                    elif 'comments' in results_data:
                        results_data = results_data['comments']
                    elif 'moderation' in results_data:
                        results_data = results_data['moderation']
                    else:
                        # If dict doesn't have expected keys, might be a single-item dict
                        print(f"   [AI] Unexpected dict structure: {list(results_data.keys())[:5]}")
                        # Try to find any list value
                        for key, value in results_data.items():
                            if isinstance(value, list):
                                results_data = value
                                break
                
                # Ensure we have a list
                if not isinstance(results_data, list):
                    print(f"   [AI] ERROR: Expected list, got {type(results_data)}")
                    print(f"   [AI] Data: {str(results_data)[:200]}")
                    raise ValueError(f"Expected list of results, got {type(results_data)}")
                
                # Convert to our format
                results = []
                for item in results_data:
                    if not isinstance(item, dict):
                        print(f"   [AI] WARNING: Skipping non-dict item: {item}")
                        continue
                        
                    results.append({
                        'comment_id': item.get('id', ''),
                        'should_hide': item.get('hide', False),
                        'reason': item.get('reason'),
                        'explanation': item.get('explanation', ''),
                        'needs_retry': False
                    })
                
                print(f"   [AI] Parsed {len(results)} results successfully")
                return results
                
            except json.JSONDecodeError as e:
                print(f"⚠️  JSON parse error: {e}")
                print(f"Response content: {content[:500]}")
                raise
            
        except Exception as e:
            print(f"❌ OpenAI API error: {e}")
            raise
    
    def test_filter(self, test_comments: List[str]) -> None:
        """
        Test the filter with sample comments (for debugging)
        
        Args:
            test_comments: List of comment texts to test
        """
        comments = [
            {
                'comment_id': f'test_{i}',
                'comment_text': text
            }
            for i, text in enumerate(test_comments)
        ]
        
        print("\n🧪 Testing Comment Filter...")
        print("=" * 60)
        
        results = self.filter_comments_batch(comments)
        
        for i, result in enumerate(results):
            comment_text = test_comments[i]
            print(f"\n📝 Comment {i+1}: {comment_text[:50]}...")
            print(f"   Hide: {result['should_hide']}")
            print(f"   Reason: {result['reason']}")
            print(f"   Explanation: {result['explanation']}")
        
        print("\n" + "=" * 60)
        
        hidden_count = sum(1 for r in results if r['should_hide'])
        print(f"📊 Results: {hidden_count}/{len(results)} comments would be hidden")


# Example test function
def test_comment_filter():
    """Test the comment filter with sample comments"""
    
    # You need to set your OpenAI API key
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        print("❌ Set OPENAI_API_KEY environment variable")
        return
    
    filter = CommentFilter(api_key)
    
    test_comments = [
        "Great post! Thank you for sharing.",
        "Bibi is destroying this country with his policies!",
        "These leftists are ruining everything",
        "You're an idiot and a traitor",
        "I served in the same unit, good memories",
        "The government should resign immediately",
        "F*** you and your stupid opinions",
        "Likud and Labor are both corrupt",
        "Thanks for your service! 🇮🇱",
        "All Arabs are terrorists"
    ]
    
    filter.test_filter(test_comments)


if __name__ == "__main__":
    test_comment_filter()
