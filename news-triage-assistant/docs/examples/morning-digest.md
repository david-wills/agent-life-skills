# Example: one morning digest

A digest composed from a real fetch of the shipped example feed list, classified
against the shipped example profile, on an ordinary news day. Three of the
profile's eight themes are shown. Every headline and URL is from the fetched
items; the two-sentence digests are the agent's. Nothing personal is in it: the
example profile is generic and the feed list is public outlets.

How it was made:

```bash
python3 news-triage-assistant/morning-news/scripts/fetch_feeds.py --feeds news-triage-assistant/morning-news/feeds.example.yaml --hours 24 --out /tmp/news_items.json
# -> 23 feeds, 2169 raw items, 2079 after cross-outlet dedupe, 293 in the window, 0 errors
# the agent reads the items and profile.example.md and writes /tmp/digest_messages.json (Steps 3-5 of prompt.md)
SKILLS_DISCORD_CHANNELS_NEWSFEED=<id> python3 news-triage-assistant/morning-news/scripts/post_digest.py --messages /tmp/digest_messages.json --dry-run
```

## The messages

One Discord message per block, in posting order, separated by rules here. A
theme that would exceed 1,900 characters splits at a bullet boundary into a
`continued` message; wire URLs carry tracking parameters, so each theme here
did. Duplicate coverage is clustered: the iPhone story is one entry with six
links, ordered by the feed list's `priority`.

**Morning Brief — Thursday, Sep 10**
_14 items · window: last 24h · sources: 7_

---

**🤖 AI**

• **Anthropic researchers resign, warning that AI development is out of control**
  A researcher quit Anthropic over what they call reckless acceleration, and the warning spread to Washington within a day. It lands while the labs are lobbying against state-level rules, so the messenger matters as much as the message. [WSJ](https://www.wsj.com/tech/ai/anthropic-researcher-quits-over-out-of-control-ai-fears-707b7628?mod=rss_Technology) · [NYT](https://www.nytimes.com/2026/09/09/technology/anthropic-researchers-raise-alarm.html) · [WaPo](https://www.washingtonpost.com/technology/2026/09/09/anthropic-researcher-resigns-warning-reckless-race-toward-superintelligence/)

• **OpenAI adds Paul Christiano to its foundation board**
  A prominent safety researcher and long-time critic of fast deployment now sits on OpenAI's nonprofit board. The clearest signal yet that the governance fight is being settled with seats, not statements. [TechCrunch](https://techcrunch.com/2026/09/09/openai-adds-a-prominent-ai-doomer-to-its-board-of-directors/) · [OpenAI](https://openai.com/index/paul-christiano-joins-openai-foundation-board)

• **A mathematician says OpenAI and Anthropic both trained on his unfinished work**
  A Navier-Stokes researcher found his draft reasoning reproduced by two frontier models and neither lab will say how. Provenance of research corpora is about to become a legal question, not an etiquette one. [NYT](https://www.nytimes.com/2026/09/10/science/tristan-buckmaster-openai-math-navier-stokes.html) · [The Verge](https://www.theverge.com/ai-artificial-intelligence/993263/where-does-openai-get-mathematics-training-data)


---

**AI continued**

• **Justice Department opens an antitrust look at Nvidia's Groq deal**
  Regulators are probing whether the acquisition takes a credible inference-chip rival off the board. Inference is where the next round of pricing power sits; this is the first deal tested on that basis. [NYT](https://www.nytimes.com/2026/09/09/business/nvidia-groq-antitrust.html)

• **Deere ships an AI assistant for farmers, and for its own sales team**
  The equipment maker's assistant answers agronomy questions and, not by accident, upsells machinery. An AI product whose value to the vendor is easier to measure than its value to the user. [WSJ](https://www.wsj.com/business/meet-deeres-jd-an-ai-assistant-aimed-at-helping-farmersand-its-sales-7d755f4b?mod=pls_whats_news_us_business_f)

---

**💻 Tech & Software**

• **Apple unveils the iPhone Duo, a $1,999 foldable, plus a new CEO and higher prices across the line**
  The three-screen foldable headlines an event that also raised prices on older models and introduced Apple's new chief executive. Whether the software justifies the hinge is the open question; every reviewer says the hardware alone does not. [WSJ](https://www.wsj.com/tech/apple-to-debut-new-foldable-iphone-new-ceo-and-new-prices-b2485b9f?mod=rss_Technology) · [NYT](https://www.nytimes.com/2026/09/09/technology/apple-iphone-duo-foldable-phone.html) · [WaPo](https://www.washingtonpost.com/technology/2026/09/09/apples-first-folding-phone-has-three-screens-costs-2000/) · [The Verge](https://www.theverge.com/tech/993300/iphone-duo-hardware-software-android-samsung-oppo) · [TechCrunch](https://techcrunch.com/2026/09/09/everything-apple-announced-at-its-fall-iphone-event-from-the-foldable-iphone-duo-to-an-always-listening-apple-watch/) · [Stratechery](https://stratechery.com/2026/the-iphone-duo-the-intelligent-personal-hub-apple-watch-audio-intelligence/)

• **Apple Watch gains a feature that listens to conversations and summarises them**
  The new audio-intelligence mode records nearby speech on request and produces recaps. The always-listening default that phones avoided for a decade just arrived on the wrist. [TechCrunch](https://techcrunch.com/2026/09/09/apple-watchs-new-feature-listens-to-your-chats-and-recaps-them/) · [TechCrunch](https://techcrunch.com/2026/09/09/apple-watchs-new-ai-features-are-normalizing-the-idea-that-technology-is-always-listening/)


---

**Tech & Software continued**

• **Zoox is taking on Waymo in San Francisco**
  Amazon's robotaxi unit is expanding its no-steering-wheel fleet in the one city where the incumbent already has scale. A second serious operator is what turns robotaxis from a demo into a market. [NYT](https://www.nytimes.com/2026/09/09/technology/zoox-waymo-san-francisco.html)

• **AI agents are flooding public services with requests**
  Agencies report automated agents filing records requests and applications faster than humans can process them. The first infrastructure to buckle under agents is not the web but the paper-era back office behind it. [TechCrunch](https://techcrunch.com/2026/09/10/ai-agents-are-flooding-public-services-with-new-requests/)

---

**📈 Business**

• **Brent crude tops $100 as the Gulf escalation shuts shipping routes**
  Houthi strikes and the US-Iran confrontation have cut Saudi exports and pushed oil past a level not seen in years. Everything downstream, from bond yields to the midterms, is now being priced off this number. [WSJ](https://www.wsj.com/finance/commodities-futures/oil-rises-as-houthi-militants-attack-amplifies-supply-disruption-fears-f0d68a35?mod=pls_whats_news_us_business_f) · [NYT](https://www.nytimes.com/2026/09/09/business/brent-oil-100-barrel-iran-war.html) · [WaPo](https://www.washingtonpost.com/business/2026/09/09/oils-rise-past-100-per-barrel-deepens-gops-midterm-challenge/)

• **Global bond yields hit multi-year highs ahead of the ECB decision**
  Oil-driven inflation fears and Treasury buybacks pushed yields up across the curve. Equities fell with them, the combination that makes the next central-bank meeting harder. [WSJ](https://www.wsj.com/finance/investing/bond-yields-edge-up-as-investors-await-ecb-rate-hike-u-s-treasury-buybacks-4cd2e9f3?mod=rss_markets_main) · [WSJ](https://www.wsj.com/finance/stocks/u-s-stocks-fall-as-war-escalation-oil-spike-continues-8e83817c?mod=rss_markets_main)

• **Porsche sells its Bugatti and Rimac stakes for $1.2 billion**
  The carmaker is exiting the hypercar side bets to widen margins in its core line. A quiet admission that the EV-halo strategy did not pay for itself. [WSJ](https://www.wsj.com/business/autos/porsche-ag-eyes-wider-margins-after-1-2-billion-sale-of-bugatti-rimac-stakes-a2c316a6?mod=pls_whats_news_us_business_f)


---

**Business continued**

• **Texas Stock Exchange is close to its first major listing from New York**
  The upstart exchange is near a deal to pull a large company's primary listing away from the NYSE and Nasdaq. Listings, not trading volume, are what make an exchange real; this would be the first proof. [WSJ](https://www.wsj.com/finance/stocks/texas-stock-exchange-is-close-to-winning-its-first-major-listing-from-new-york-569c0339?mod=rss_markets_main)

• **HSBC begins a search for a new finance chief**
  The CFO's departure was announced with no successor named. An unplanned CFO exit at a bank this size is itself the story. [WSJ](https://www.wsj.com/business/c-suite/hsbc-chief-financial-officer-to-leave-in-2027-dfab7c9a?mod=pls_whats_news_us_business_f)

## What the poster's dry run says

Validation runs before anything is sent: the character count of every message
against Discord's 2,000 cap, the channel it would use, and where a resumed run
would start.

```json
{
  "ok": true,
  "dry_run": true,
  "channel": "123456789012345678",
  "messages": 7,
  "would_start_at": 0,
  "chars": [
    79,
    1654,
    783,
    1606,
    741,
    1586,
    763
  ]
}
```
