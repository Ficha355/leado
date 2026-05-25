import os
from dotenv import load_dotenv
load_dotenv()

def test_youtube():
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"])
        resp = yt.search().list(q="freelance video editor", part="snippet", maxResults=1).execute()
        title = resp["items"][0]["snippet"]["title"]
        print(f"  YouTube  ✓ OK — premier résultat : « {title[:60]} »")
    except Exception as e:
        print(f"  YouTube  ✗ ERREUR — {e}")

def test_serpapi():
    try:
        from serpapi import GoogleSearch
        search = GoogleSearch({
            "q": "freelance video editor",
            "api_key": os.environ["SERPAPI_KEY"],
            "num": 1,
        })
        data = search.get_dict()
        title = data["organic_results"][0]["title"]
        print(f"  SerpApi  ✓ OK — premier résultat : « {title[:60]} »")
    except Exception as e:
        print(f"  SerpApi  ✗ ERREUR — {e}")

def test_anthropic():
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": "say hello"}],
        )
        reply = msg.content[0].text.strip()
        print(f"  Anthropic  ✓ OK — réponse : « {reply} »")
    except Exception as e:
        print(f"  Anthropic  ✗ ERREUR — {e}")

if __name__ == "__main__":
    print("\nTest des clés API Leado\n" + "─" * 40)
    test_youtube()
    test_serpapi()
    test_anthropic()
    print("─" * 40 + "\n")
