"""
clef_comment_filter.py — Comment moderation via Cloudflare Workers AI's Clef-flash
decision model: a probability classifier, not a text generator. You send it a
piece of content ("state") plus a schema of typed questions, and it returns a
probability for each question instead of free-form text.

Confirmed from Cloudflare's own docs (developers.cloudflare.com/workers-ai/models/clef-flash/):
  - Model id:  @cf/cloudflare/clef-flash
  - Endpoint:  POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}
  - Auth:      Authorization: Bearer <api_token>
  - Request:   {"state": "<content>", "questions": {<id>: {"type": "noul"|"choice"|"score", "instructions": "..."}}}
  - Pricing:   ~$0.038 / million input tokens
  - Context:   24,576 tokens shared between state + questions

Response shape (confirmed via a live call, 2026-10-26):
  {"result": {"model": "clef-flash",
              "answers": {<id>: {"type": "noul", "noul": 0.894}, ...},
              "usage": {"input_tokens": N, "output_tokens": 0}},
   "success": true, "errors": [], "messages": []}
The probability lives under a key matching the question's own `type` value
(e.g. a "noul" question's answer has its score under the "noul" key) —
_extract_probability() checks that first, with a few generic names as
fallback, and logs loudly if a future response shape doesn't match either.

Same public interface as ai_comment_filter.CommentFilter.filter_comments_batch()
so it's a drop-in swap behind the `comments_filter_engine` config key — see
ai_comment_filter.build_comment_filter().

One difference from the OpenAI engine: Clef's schema scores one "state" per
call (the questions are about that one piece of content), so this makes one
API call per comment rather than batching many comments into one call.
"""
from typing import Dict, List, Optional

import requests

# Keep these keys ('political' / 'hate' / 'spam') in sync with the rest of the
# app — they're used as filter_reason values throughout database.py/routes_comments.py.
CATEGORY_QUESTIONS = {
    'political': "Is this comment focused on partisan politics, politicians, political "
                  "parties, or government policy debates (rather than general military/"
                  "army experience, which is the normal topic of this page)?",
    'hate':      "Does this comment contain slurs, hate speech, personal attacks, or "
                  "discriminatory language?",
    'spam':      "Is this comment spam — a commercial advertisement, repeated or "
                  "bot-like content, or an off-topic promotion?",
}


class ClefCommentFilter:
    API_BASE = "https://api.cloudflare.com/client/v4"
    MODEL = "@cf/cloudflare/clef-flash"

    def __init__(self, account_id: str, api_token: str, db=None, threshold: float = 0.7):
        self.account_id = account_id
        self.api_token = api_token
        self.threshold = threshold
        self.db = db  # kept for interface parity with CommentFilter; unused here

    def _run(self, state: str) -> dict:
        url = f"{self.API_BASE}/accounts/{self.account_id}/ai/run/{self.MODEL}"
        headers = {"Authorization": f"Bearer {self.api_token}"}
        body = {
            # Stay well under the 24,576-token shared context window.
            "state": state[:4000],
            "questions": {
                cat: {"type": "noul", "instructions": instructions}
                for cat, instructions in CATEGORY_QUESTIONS.items()
            },
        }
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _extract_probability(self, answer) -> Optional[float]:
        """Best-effort extraction of a yes-probability from one answer value.
        Returns None (logged) if the shape isn't recognized, rather than
        silently defaulting to a value that could hide real content."""
        if isinstance(answer, (int, float)):
            return float(answer)
        if isinstance(answer, dict):
            # Confirmed live shape: {"type": "noul", "noul": 0.894} — the
            # probability lives under a key matching the question's own
            # 'type' value. Check that first, then fall back to generic names.
            type_key = answer.get('type')
            if type_key and isinstance(answer.get(type_key), (int, float)):
                return float(answer[type_key])
            for key in ('noul', 'probability', 'p', 'yes', 'value', 'score', 'confidence'):
                val = answer.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
            if isinstance(answer.get('answer'), bool):
                return 1.0 if answer['answer'] else 0.0
        return None

    def filter_comments_batch(self, comments: List[Dict], batch_size: int = 50) -> List[Dict]:
        """Same contract as CommentFilter.filter_comments_batch(): one entry in,
        one result out, per comment. `batch_size` is accepted for interface
        parity but unused — Clef scores one comment ("state") per call."""
        results = []
        for comment in comments:
            comment_id = comment['comment_id']
            text = comment.get('comment_text', '')
            try:
                raw = self._run(text)
                # Workers AI wraps model output in a "result" envelope.
                answers = raw.get('result', raw).get('answers', {})

                scores = {}
                for cat in CATEGORY_QUESTIONS:
                    prob = self._extract_probability(answers.get(cat))
                    if prob is None:
                        print(f"   [clef] ⚠️ unrecognized '{cat}' answer shape for "
                              f"comment {comment_id}, treating as 0: {answers.get(cat)!r}")
                        prob = 0.0
                    scores[cat] = prob

                worst_cat, worst_score = max(scores.items(), key=lambda kv: kv[1])
                should_hide = worst_score >= self.threshold
                # Confidence in whichever decision was made: how sure it's a
                # violation when hiding, how sure it's clean when not.
                confidence = worst_score if should_hide else (1.0 - worst_score)

                results.append({
                    'comment_id': comment_id,
                    'should_hide': should_hide,
                    'reason': worst_cat if should_hide else None,
                    'explanation': f"clef-flash scores: {scores}",
                    'confidence': confidence,
                })
            except Exception as e:
                print(f"   [clef] ❌ error scoring comment {comment_id}: {e}")
                results.append({
                    'comment_id': comment_id,
                    'should_hide': False,
                    'reason': None,
                    'explanation': f'Error: {e}',
                    'needs_retry': True,
                })
        return results

    def test_connection(self) -> bool:
        """Return True if Cloudflare accepts a minimal scoring call."""
        try:
            self._run("test")
            return True
        except Exception as e:
            print(f"[clef] ❌ connection test failed: {e}")
            return False
