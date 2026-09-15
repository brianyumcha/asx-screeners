"""
Shared card-grid HTML dashboard renderer, used by both OBV SCREENER.py and
PULLBACK SCREENER.py so the two reports look and behave consistently
(styled after traders-hub.luk.com.au/pre-breakout-screener - card grid with
a mini candlestick+SMA chart per stock, sector/size/timeframe filters,
ranked/sector view toggle, cooldown history reveal).

This is a STATIC file only - there's no backend, no live "run scan now", no
accounts. Every value baked into the page comes from the scan that produced
it. Re-running the screener script overwrites the file with a fresh scan.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def size_bucket(market_cap):
    if market_cap >= 2_000_000_000:
        return 'L'
    if market_cap >= 300_000_000:
        return 'M'
    return 'S'


def render_dashboard_html(
    cards,               # list of dicts, see card schema below
    excluded_cards,       # cooldown-hidden cards, same schema, shown via "already seen" toggle
    total_scanned,
    title,
    subtitle,
    footer_note,
    out_path,
):
    """
    Card schema (each item in `cards` / `excluded_cards`):
      {
        'ticker': str, 'sector': str, 'market_cap': int,
        'price': float, 'change_1d': float, 'score': int (0-100),
        'stats': [ {'label': str, 'value': str}, ... up to 3 ],
        'dates': [...], 'opens': [...], 'highs': [...], 'lows': [...],
        'closes': [...], 'volumes': [...],   # trailing ~260 daily bars
      }
    """
    for c in cards:
        c['size'] = size_bucket(c['market_cap'])
        c['already_seen'] = False
    for c in excluded_cards:
        c['size'] = size_bucket(c['market_cap'])
        c['already_seen'] = True

    all_cards = cards + excluded_cards
    sectors = sorted({c['sector'] for c in all_cards})

    now = datetime.now(SYDNEY_TZ)
    session_line = (
        f"Session {now.strftime('%Y-%m-%d')} · {total_scanned} scanned · "
        f"{len(cards)} new · {len(excluded_cards)} already seen · "
        f"last run {now.strftime('%Y-%m-%d %H:%M')} Sydney time"
    )

    active_href = 'pullback.html' if 'Pullback' in title else 'pre-breakout.html'
    html = HTML_TEMPLATE
    html = html.replace('##NAV##', render_nav(active_href))
    html = html.replace('##TITLE##', title)
    html = html.replace('##SUBTITLE##', subtitle)
    html = html.replace('##SESSION_LINE##', session_line)
    html = html.replace('##FOOTER_NOTE##', footer_note)
    html = html.replace('##SECTORS_JSON##', json.dumps(sectors))
    html = html.replace('##DATA_JSON##', json.dumps(all_cards))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path


NAV_ICON_B64 = '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'


NAV_SCREENER_LINKS = [
    ('higher-high.html', 'Higher-High'),
    ('pre-breakout.html', 'Pre-Breakout (OBV)'),
    ('pullback.html', 'Pullback (Zag Zone)'),
]
NAV_INDEX_LINKS = [
    ('energy-index.html', 'Energy'),
    ('healthcare-index.html', 'Healthcare'),
    ('materials-index.html', 'Materials'),
    ('tech-index.html', 'Tech'),
]


def render_nav(active_href):
    def group(label, links):
        items = "\n".join(
            '<a href="{0}"{1}>{2}</a>'.format(href, ' class="active"' if href == active_href else '', text)
            for href, text in links
        )
        return (
            '<div class="sitenav-drop"><button class="sitenav-toggle" type="button">{0} '
            '<span class="sitenav-caret">&#9662;</span></button>'
            '<div class="sitenav-menu">\n{1}\n</div></div>'
        ).format(label, items)
    return (
        '<nav class="sitenav">\n'
        '<img class="navicon" src="data:image/png;base64,' + NAV_ICON_B64 + '" alt="">\n'
        '<a class="sitenav-brand" href="index.html">\n'
        '<span class="brand-main">Brian Yum Cha &mdash; ASX Trading</span>\n'
        '<span class="brand-tag">Reading the charts. Timing the carts.</span>\n'
        '</a>\n'
        '<span class="sitenav-divider"></span>\n'
        '<div class="sitenav-links">\n'
        + group('Screeners', NAV_SCREENER_LINKS) + "\n"
        + group('ASX Sector Indexes', NAV_INDEX_LINKS) + "\n"
        + '<a class="sitenav-toggle" href="insider-index.html">Insider Buying</a>\n'
        + '</div>\n</nav>\n'
        '<script>\n'
        "document.querySelectorAll('.sitenav-drop').forEach(function(drop){\n"
        "  var toggle = drop.querySelector('.sitenav-toggle');\n"
        "  toggle.addEventListener('click', function(e){\n"
        "    e.stopPropagation();\n"
        "    var willOpen = !drop.classList.contains('open');\n"
        "    document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "    if (willOpen) drop.classList.add('open');\n"
        "  });\n"
        "});\n"
        "document.addEventListener('click', function(){\n"
        "  document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "});\n"
        "document.addEventListener('keydown', function(e){\n"
        "  if (e.key === 'Escape') document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "});\n"
        '</script>\n'
    )


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>##TITLE##</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAPH0lEQVR4nO1ba4xd11X+1t77nHPvnTuPO+/xMw0BUqdEqiUUqAIuaRpTyh9aOYIfIPVHhVq1oLa4iQPiehCipSFIUAmpRUJqo0rI04YfUBXJVV6taGmTAHFjXNrGsRNnbM943nfuveexN1prn3Pn2nFCxpk7jkOXdH3O7LMfa3177fU6x8BP6fqRc3XFv+vIwk/pulCx6/OP3f8h/nW3bTep67Eovjir5Up0UH7dbW81AFydzzmIxXX572luh7St8q+7LW8nHsNj8Vam+SeP/D3/ricP1KuJ3bFDmu6dyS4+fv+hWn/5/pW1NuuB4gUdHJFsMvbkTJztauMOdqAa0eJq87Pj7/7sTDFXL/g06BWN7RNwFeG9pi/aH7QTBPpyjY4TL1MY6JHu9iSzMH0R1FrzvQBmirluLAByUqQskgxJYl1uCzqkRB+AOLXdzUhT65BkJGN7TKZH89LT/zNLxwCdtJNnk2Y8B2dtklhNgGNV504Z+SucEyT4GLARJCBLmrHisTwHz5UfV7fljGKLqM4e5QDU0XE4NYPsMk6NkrMg7POK7ZQOAPp2/K24vmfxB9kTgEVkbKePdUCXZkjTIeijF0F4Anaa+78ZAKgD6jaA7gW6jRT93vDwjp+p6ptqpfBtobU7DdFkqNSoIgwaUAVEEcFF3NnBteGoncE1MoeV2Nr5xLnZWKnZpTR+/idL2QtfXlh4uVsDHKCOAnijQNAbGXwI0DO54IdqtT37a+Fd40FwV3+g95c13VRVuq9PK4SKzT9dpsOuuMt33D/d0HPrHGLr0Mgs1tKs0XTuheUke2YuSR57aqH96CNLS2eu5GFbAagDitH/+M7hX9odhZ/cVQ7ed1Mpqla0EgES65A5tntgtfbuTcTmI95Zma7Eg6RrDgdJZ6WJKFC+bT2zONOK115sxt94sR3/9efPLXy34GXbAKjnCx7eM/6nuyI9fXu1jH6jWejMemFVLkMu7ZWrEogFMt4GuzSF4zOfw9RNHT1hCwlYHhYo0itphhOrTbwUZ/UHz178s2sFgTY7oFC5T+0d+8CkMV/bEWq7uxRZrUi/rvlYeK2hotALzU2KYNsxXJZdFYSrECOdvdRqq3Nxps6n6QcfOjP3CHuMK2zR/0kKm6R9uZDa4aAhWAVkjmDkGHcJ+Qos+E+VC1+KQFGE0Q+8X358L21ae2/RNZTh4BiRf9xemIx8zYx5YF6473PXsKFmswNuy7XSOTfC51MRWdZ3V+yuMMq48umX4+8Ps2JXqEBGQ1XK0OUyBn/1XTLnyrf/HVmz6TUgZUW3HDZ43BTBCSgkGkPyzMkZUySrKealm7eeAvDcxiKTbNl1LrNsi1JQxsiVzZ8IxILkak+hge7vRzA6DDM46J8DiPbsQrq8jMQYZKurcHEK2MzPYRRcYHymwP3jFOSsQM5rMw8gTFzBW8+OALGhYYNDoFFv5bz8ssvaA7DrPe9B/+49AooKAlDAglcR7ZhCODEG01/1Ks9gGSP33MbPwh1T0lcMZBjIvM3b9+LSRw/CRQGc0R6M3En43JnGuozgpo6BwjVQZWSkjwg1HuyVs7DsWgSOhodhqhWoIAQFAczQIMKpCahy2WuCCaDCUNjnn9ybQJ7x0eC+PIYYgCiArVWR7BqBLYUA24l8UVnSL11jnq5FFrWZzvUc3ZWhaAjAIDOhN3bDH4EgENUmbUQwPVBFMDYqz4rdVn0V6IF+LwwLPdAvbYVWsK0Ixkd9n1IEGA1Ks1wD8rCaDbEAKEwN5jx1eOxpMtROk5GIKFKAU3IKFMDqH+Q7y/dhAFPtgx6p+YPJ4EQhKvtuReW2nxdBTb/ftIE775BYYP3kD7H+g1NwzhtBPTGCdGkJ4J1XGq4cwq0HcK2EfU+hAU4TRS5N2BC+uFlZzGY6c8wvNxbjgSFWHyeWmC08q3q5jKhWE1UOqv0oTU7ABhpZqy12gHe5/479CHftgIuTjs9nI8jqrqt9aJ0+C5umsDYF9ZWAwRKy4X7RgmTnMChJQQKAFQ1gJxEoovUUY5fx2AsAnitiAKJxIx6ArFKkyHj1HnnHO9C/d6+o9cDNb0NfcAsWTp2CTZKO/xd3xkFPysfEn0C+9xEw+T4NLTucDVexfvcvoB1ZOLJY+u07UX7meQzO/BucAKGgQdYQKUd2spvH3hjBA/5i4aY4sefBEvMqBR1FiIaGNoxbECDoq8KUSz4GEH31186voK62Il5wWiEbqMAN9UEF3i7wUUh21GD7In/kGDDeRT/VZDePvQFggyZ1XuATPwwFpbX47Y4n5p2WOi8L3SX4a25QAYS3GY4dPQc/RXLEJSVONgQQYgw8Dz4Qm8I1kNpU7ydyNh2mCuHzQKQrCtyQ5TKBNktFKpV7mYI6UWHueTob4XINyHnslQbY/Drh1T8PeiVgz3PeIpmRRCfP8K6lkJUP9/NuTCBFtHzOHIsi8J64gsetB2A6n9wRxorcRipd7LYye3nuK+ltwewmQZCKiPVxP6cGPhvulA04JuA5i03IlU28wHQPASD+56Fdu8oEDBdnTyo4XPyI24jX1uRedizLYFstpK0WnOWYvwDhtZDYAI3Yza22YNfbsM76R9bBLKxCNdriBn0U6I8A88S8dfO6pW7Q5bOu6PYgnBsS0X0gBpel4uounTiB9fPnMfrOd6IxO4vG/EXY0Pi8v8jwOhpRzNitJU768M5zfzW/gsqxJ5G+fRLNt+/A0NefRvj8BVA7ES3IS8hiJQgYuqRaHA02u2beOgCO5uW62OlhRbaSu0ABwDLDaYpkbc3Lk6aIl5fRPPcyVG0IqhxJeGzjuOMiNyAFJI7gpIlI+kiMwJq0uAxaXYYeKQG3jCN4/gL0pVUBANwn5yHXhIpzugZgtuB1SwE42YkC07HAaNJcDCHxzp3UlwVnTYCzcuUIMLs4BzM6LBmeXWug8YNTqOSWXWJ9fj+wsircchjMfVgTbGMd6cKCODhqx2JTVCsGxX73fU3AG0LmJSBSaZaOXcbrVgKwL5/UKY4CZaBlHjbq1B4EAYCUByNui47G5y8iZH8+rLH21H+i+ZPTMAMDGLrrThm69Oi3ka6sIFtclnHp8iriuTnYwnYkmY8JRPX5eBQlVgkYhReOzBNF4928bikAHXJup1wJaFkrZesBo5FKkGJBSYK00UDWasFyvM86kqRoz56Xqg8XQ6SNbUKaylSc8GSrDQEhmV9AurScAykWFmq1CbPYgOIcgHffOQRE4MJoWwounjVybgc2SWazA8TrOWAptdgZKVxox1AUosqRYK4BZ48f71R7CkEYnGT+ErK1NZjhVQRDgz4h4neD52aRLC0jXViEbbXFBojnYMGMRum/XkB48kWgnUClFlx9XUszWZtDcubl9dVS3xgA1iNgjqfOYjF1VNHW1Yyi2VaMQaMxFAbgVz0CRB7AFLW9wsL7vwnZWgNZsyWPWi+9LGBk602fGHE//3pA9FzbtjDKVj9OU8zHKVaSVIRfTKxbEFAYZXP8Ml5fB9G1lMTv2zP6FwPGHFlNs3Q8NDQWaOZFfGJFa1SNRqSUZ7orOvJ1Ae8FVKkklR+mePaCxAyOtUVi/w3i0IcPihy3NMO6HAE/11ycZReT1PUbbVbS9DN/eXb+gc2+KaLNAMD9DwGKFziyd/xzJUWHE94hIBs2Cv1GqSCvEXKuzueU3+gEXOHxBVRfxZHaYXDZ4lmaSDSZOX6jBLEpMR8bflMubT4KTJxzq6m1vOsZoAOxRe7Bz5y5+OlceN59ty1vhh7YO/o+A1UnojuQr1pSZKuKbKSIQiU+miu3PmfM3wH6HCdPfcXOeZX3O+s6UTR/UcChU2yda1vn1qxTLctFKM945tz3Y9j6587Mf2Pb3gwV1K1qf7Jn4jeJ3O9a4C5NNJonJyIoG6yAyAbErsprgYTQXUlikTJwzF/sfuKgEueUOML8owEGJnNuXgGPOkcP//nZC1/nx2/kBSldy6CrgcBUn5oatSX7y866O53DL1rCzzmHSaO4RpTHza+xaOfNMQvKZ98iVYTz5PAjEJ4yip5ES313enZ2/tV4uC4fSBzyG40rGalPTVVQol2mhD1Zip3WuUnr3DBAg4AtkSNJXhy5Jts5Aq0Q0SVFdF4bnHNtOpM17bnp2dn1V1lvU+f9akTYQmJNnQFUXpfbsq845OsT/yEGq7vNXxG+yT+Tc94D/vhrnxobn4h+pdW2ChnzvkGFurziE1GtXClS9uKF9rdu+eBDc7kN2PLvg3r7ldjjdQ1Mp0Mj4eH+iaE/ChcbMFd8JvdqlGYWUa0Pcbr4VwAO4/G64bluyM/ktKIy1lrtRjPm45C/TRNy+d/2Km1xFGgjY3tMptcLZBz6DlWisJWgWg6RcqjLCxuNVjtFKTJI0kyKq1ortOOUrwGGKsiWL7N9PSHq1cTOOQ5/3MXHD//sQF/lt+YWGvf0lYJvtpP0VutIlUN9cr2V/XqlpP+1GWf7jaZLWqtzzVZ6oFoOj5fLBiuN9X8af/eDPyrmwo1Mz/zjR47I9Ssf/Y3vffnD8mXEs1/9WJ2v35e2j7yru992kerl5Lxz9Xpdnfrqx24NjFLusboJS2p3VAkM3weKKnwtRVpXKuomadO69MN//sROvt+O/0Shejr70aM0PT1t247L6HqOfm2aM/dQW6zwPYgyuTq7ap0q+edYiuNklO+P8peQNzQAt50sPvebAmXn/b2KSJlGXkASSjLbUIBYfALmrIVUdm7Lx/eSFLaDnJ2IEzvLt9a6MLbtdcwcUi7/7wFQpmFJailwULMEK+/5Dm0Da6qXkx99bp+vgQQ6DKt9/PECW3MTIG3i5ppiBPg5ZfE6rBUATAlnjTYVvp9B78n0auLcddl/+OQ9u5975vSn27b1CDcHoWlmN+9a+87JFTOg+Js4UFCrrJjYtnncie89PwRHf/yF++5++N57Z5bzkNrdcBpAee5b7R9cbLWSI6XFeO5L992958TTP/74d774rdqdv/M3zXI55Dcp7j+e+O/JZ586/Ydfuf/9tayB0624/UCtVOMMsWc5QIdPbCMd+8Q9wy2jPhxj5e9K4dBwOQq/ubC4fkeknLGKPtSuhJ///el/6X34t9107NihVyR8Dz9wcOpLhw8++IX77h684hFdrf9bhaheP2A20f6WJnrs/6HQeLPR/wJAeSe+MfR+CwAAAABJRU5ErkJggg==">
<script>
// Set before first paint so there's no flash of the wrong theme. Defaults
// to dark (this site's original look) unless the viewer explicitly chose
// light on a previous visit.
try {
  if (localStorage.getItem('theme') === 'light') document.documentElement.setAttribute('data-theme', 'light');
} catch (e) {}
</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
:root {
  --bg:#121316; --surface:#1c1f24; --border:#454b56; --card:#262b32;
  --accent:#e8b355; --accent-ink:#1c1204; --accent2:#8fc4db; --accent2-ink:#0a1f26;
  --good:#a8d491; --bad:#f0998a; --warn:#eec27a;
  --text:#f5f2e8; --muted:#c9c2b0;
}
[data-theme="light"] {
  --bg:#faf8f2; --surface:#f0ead8; --border:#a89b7a; --card:#e6ddc4;
  --accent:#7a4a0f; --accent-ink:#fdf8ef; --accent2:#163540; --accent2-ink:#eaf5f7;
  --good:#33481f; --bad:#6b241c; --warn:#6b4a0f;
  --text:#141209; --muted:#3d3829;
}
.themebtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  width:2.3rem;height:2.3rem;border-radius:50%;cursor:pointer;font-size:1.05rem;
  display:flex;align-items:center;justify-content:center;flex:none}
.themebtn:hover{border-color:var(--accent2)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  font-variant-numeric:tabular-nums;margin:0;}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(232,179,85,.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(232,179,85,.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0}
.wrap{max-width:1560px;margin:0 auto;position:relative;z-index:1;padding:0 1.6rem 1.6rem}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
h1{font-family:"Fraunces",Georgia,serif;font-size:2.2rem;font-weight:600;letter-spacing:-.01em;
  text-wrap:balance;color:var(--text)}
@media (max-width:640px){h1{font-size:1.8rem}}
.subtitle{font-size:.92rem;color:var(--muted);margin-top:.3rem;max-width:44rem}
.session{font-size:.68rem;color:var(--muted);margin:.6rem 0 1.1rem;letter-spacing:.03em}
.copybtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.72rem;padding:.5rem .9rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.4rem;white-space:nowrap;height:fit-content}
.copybtn:hover{border-color:var(--accent)}
.copybtn.copied{border-color:var(--accent);color:var(--accent)}
.topbar-right{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:.5rem .6rem}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.7rem}
.pillgroup{display:flex;gap:.3rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.2rem}
.pill{background:transparent;border:none;color:var(--muted);font-size:.72rem;
  padding:.4rem .8rem;border-radius:6px;cursor:pointer;white-space:nowrap}
.pill:hover{color:var(--text)}
.pill.active{background:var(--accent2);color:var(--accent2-ink);font-weight:600}
input[type=text]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.75rem;padding:.5rem .8rem;border-radius:8px;outline:none;width:170px}
input[type=text]:focus{border-color:rgba(232,179,85,.4)}
.checkline{display:flex;align-items:center;gap:.4rem;font-size:.72rem;color:var(--muted);cursor:pointer}
.checkline input{cursor:pointer}

.sectorrow{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.1rem}
.sectorpill{background:var(--surface);border:1px solid var(--border);color:var(--muted);
  font-size:.68rem;padding:.35rem .7rem;border-radius:20px;cursor:pointer}
.sectorpill:hover{color:var(--text)}
.sectorpill.active{background:rgba(232,179,85,.12);border-color:var(--accent);color:var(--accent)}

.countline{font-size:.7rem;color:var(--muted);margin-bottom:.8rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
.sectionhead{grid-column:1/-1;font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  color:var(--text);margin:1.4rem 0 .2rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.sectionhead:first-child{margin-top:0}

.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.9rem;
  display:flex;flex-direction:column;gap:.5rem}
.card.seen{opacity:.55}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.cardhead-left{display:flex;align-items:baseline;gap:.4rem}
.ticker{font-family:"IBM Plex Mono",monospace;font-weight:700;font-size:1.02rem;color:var(--text)}
.ticker a{color:inherit;text-decoration:none}
.ticker a:hover{color:var(--accent2)}
.chg{font-size:.72rem;font-weight:600}
.up{color:var(--good)} .dn{color:var(--bad)} .neutral{color:var(--muted)}
.scorebar{display:flex;align-items:center;gap:.4rem}
.scorebar .bg{width:42px;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.scorebar .fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent2),var(--accent))}
.scorebar .num{font-size:.68rem;color:var(--muted)}

.chartwrap{position:relative;width:100%;height:150px}
canvas{width:100%;height:100%;display:block}

.statrow{display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);
  border-top:1px solid var(--border);padding-top:.5rem}
.statrow .v{color:var(--text)}
.sectortag{font-size:.64rem;color:var(--muted);text-transform:none}

.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:3rem 0;font-size:.85rem}
.empty.table-empty{text-align:center;color:var(--muted);padding:3rem 0;font-size:.85rem}

table.datatable{width:100%;border-collapse:collapse;font-size:.8rem}
table.datatable thead tr{border-bottom:1px solid var(--border)}
table.datatable th{text-align:left;padding:.6rem .8rem;font-size:.66rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;cursor:pointer;white-space:nowrap;user-select:none;font-weight:600}
table.datatable th:hover{color:var(--text)}
table.datatable th.sorted{color:var(--accent)}
table.datatable tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
table.datatable tbody tr:hover{background:rgba(232,179,85,.05)}
table.datatable td{padding:.65rem .8rem;vertical-align:middle;white-space:nowrap}
table.datatable td.ticker-cell{font-family:"IBM Plex Mono",monospace;font-weight:700}
table.datatable td.ticker-cell a{color:var(--accent2);text-decoration:none}
table.datatable td.sector-cell{color:var(--muted)}
.notice{background:rgba(232,179,85,.08);border:1px solid rgba(232,179,85,.25);border-radius:4px;
  padding:.7rem .9rem;font-size:.68rem;color:var(--warn);margin-bottom:1rem;line-height:1.6}
footer{margin-top:2rem;font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:1rem}
.sitenav{position:sticky;top:0;z-index:500;display:flex;align-items:center;gap:1.4rem;
  background:var(--card, var(--surface2, var(--surface)));border-bottom:1px solid var(--border);
  padding:.7rem 1.1rem;margin-bottom:1.5rem;font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  box-shadow:0 2px 10px rgba(0,0,0,.12)}
.navicon{display:block;flex:none;width:42px;height:42px;border-radius:8px}
.sitenav-brand{display:flex;flex-direction:column;gap:.05rem;text-decoration:none}
.sitenav-brand .brand-main{font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  letter-spacing:.02em;color:var(--accent);white-space:nowrap}
.sitenav-brand .brand-tag{font-family:"Fraunces",Georgia,serif;font-style:italic;font-size:.62rem;
  color:var(--muted);white-space:nowrap}
.sitenav-brand:hover .brand-main{opacity:.8}
.sitenav-divider{width:1px;height:1.3rem;background:var(--border);flex:none}
.sitenav-links{display:flex;gap:.2rem}
.sitenav-drop{position:relative}
.sitenav-toggle{background:transparent;border:none;color:var(--text, var(--ink));
  font-family:inherit;font-size:.8rem;padding:.5rem .65rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.3rem}
.sitenav-toggle:hover,.sitenav-drop.open .sitenav-toggle{background:var(--bg);color:var(--accent)}
.sitenav-caret{font-size:.85rem;opacity:1;color:var(--accent);display:inline-block;
  transition:transform .15s ease}
.sitenav-drop:hover .sitenav-caret,.sitenav-drop.open .sitenav-caret{transform:rotate(180deg)}
.sitenav-menu{position:absolute;top:100%;left:0;margin-top:.3rem;min-width:190px;
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.25);padding:.3rem;
  opacity:0;visibility:hidden;transform:translateY(-4px);
  transition:opacity .12s ease,transform .12s ease,visibility .12s}
.sitenav-drop:hover .sitenav-menu,.sitenav-drop.open .sitenav-menu{
  opacity:1;visibility:visible;transform:translateY(0)}
.sitenav-menu a{display:block;padding:.5rem .6rem;border-radius:6px;font-size:.78rem;
  color:var(--text, var(--ink));text-decoration:none;white-space:nowrap}
.sitenav-menu a:hover{background:var(--bg);color:var(--accent)}
.sitenav-menu a.active{color:var(--accent);font-weight:600}
.sitenav-toggle.active{background:var(--bg);color:var(--accent)}
@media (max-width:640px){
  .sitenav{padding:.5rem .7rem;gap:.7rem}
  .sitenav-brand{font-size:.7rem}
  .sitenav-toggle{font-size:.74rem;padding:.45rem .5rem}
  .sitenav-menu{min-width:170px}
}
</style>
</head>
<body>
##NAV##

<div class="wrap">
  <div class="topbar">
    <div>
      <h1>##TITLE##</h1>
      <div class="subtitle">##SUBTITLE##</div>
    </div>
    <div class="topbar-right">
      <button class="copybtn" id="copyBtn">📋 Copy TradingView list</button>
      <button class="themebtn" id="themeBtn" title="Toggle light/dark">🌙</button>
    </div>
  </div>
  <div class="session">##SESSION_LINE##</div>

  <div class="notice">⚠ Static report from a single scan run — not live. Re-run the script for fresh data. Nothing here is a trade recommendation.</div>

  <div class="controls">
    <div class="pillgroup" id="modeToggle">
      <button class="pill active" data-mode="chart">📊 Charts</button>
      <button class="pill" data-mode="table">☰ Table</button>
    </div>
    <div class="pillgroup" id="viewToggle">
      <button class="pill active" data-view="ranked">Ranked</button>
      <button class="pill" data-view="sector">Sector</button>
    </div>
    <div class="pillgroup" id="sizeToggle">
      <button class="pill active" data-size="S">S</button>
      <button class="pill active" data-size="M">M</button>
      <button class="pill active" data-size="L">L</button>
    </div>
    <div class="pillgroup" id="tfToggle">
      <button class="pill" data-tf="63">3M</button>
      <button class="pill active" data-tf="126">6M</button>
      <button class="pill" data-tf="252">12M</button>
    </div>
    <input type="text" id="search" placeholder="Search ticker...">
    <label class="checkline"><input type="checkbox" id="showSeen"> Show already seen</label>
  </div>

  <div class="sectorrow" id="sectorRow"></div>
  <div class="countline" id="countLine"></div>
  <div class="grid" id="grid"></div>
  <div id="tableWrap" style="display:none;overflow-x:auto;"></div>

  <footer>##FOOTER_NOTE##</footer>
</div>

<script>
const themeBtn = document.getElementById('themeBtn');
function isLightTheme() { return document.documentElement.getAttribute('data-theme') === 'light'; }
function syncThemeBtn() { themeBtn.textContent = isLightTheme() ? '☀️' : '🌙'; }
syncThemeBtn();
themeBtn.addEventListener('click', () => {
  const next = isLightTheme() ? null : 'light';
  if (next) document.documentElement.setAttribute('data-theme', next);
  else document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('theme', next || 'dark'); } catch (e) {}
  syncThemeBtn();
  render();  // re-run chart-mode canvas drawing with theme-correct colors
});

const DATA = ##DATA_JSON##;
const SECTORS = ##SECTORS_JSON##;

let state = {
  mode: 'chart',   // 'chart' or 'table'
  view: 'ranked',
  sizes: new Set(['S','M','L']),
  tf: 126,
  search: '',
  showSeen: false,
  sector: null,
  sortKey: 'score',
  sortDir: -1,
};

const sectorRow = document.getElementById('sectorRow');
sectorRow.innerHTML = '<button class="sectorpill active" data-sector="">All sectors</button>' +
  SECTORS.map(s => `<button class="sectorpill" data-sector="${esc(s)}">${esc(s)}</button>`).join('');
sectorRow.querySelectorAll('.sectorpill').forEach(el => el.addEventListener('click', () => {
  state.sector = el.dataset.sector || null;
  sectorRow.querySelectorAll('.sectorpill').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  render();
}));

document.getElementById('modeToggle').addEventListener('click', e => {
  if (!e.target.dataset.mode) return;
  state.mode = e.target.dataset.mode;
  document.querySelectorAll('#modeToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  document.getElementById('viewToggle').style.display = state.mode === 'chart' ? '' : 'none';
  document.getElementById('tfToggle').style.display = state.mode === 'chart' ? '' : 'none';
  render();
});
document.getElementById('viewToggle').addEventListener('click', e => {
  if (!e.target.dataset.view) return;
  state.view = e.target.dataset.view;
  document.querySelectorAll('#viewToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('sizeToggle').addEventListener('click', e => {
  const sz = e.target.dataset.size;
  if (!sz) return;
  if (state.sizes.has(sz)) state.sizes.delete(sz); else state.sizes.add(sz);
  e.target.classList.toggle('active');
  render();
});
document.getElementById('tfToggle').addEventListener('click', e => {
  if (!e.target.dataset.tf) return;
  state.tf = parseInt(e.target.dataset.tf, 10);
  document.querySelectorAll('#tfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('search').addEventListener('input', e => {
  state.search = e.target.value.toUpperCase();
  render();
});
document.getElementById('showSeen').addEventListener('change', e => {
  state.showSeen = e.target.checked;
  render();
});
document.getElementById('copyBtn').addEventListener('click', () => {
  const visible = filteredCards();
  const text = visible.map(c => `ASX:${c.ticker}`).join(',');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.classList.add('copied');
    btn.textContent = `✓ Copied ${visible.length} tickers`;
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 Copy TradingView list'; }, 1800);
  });
});

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function filteredCards() {
  return DATA.filter(c =>
    (state.showSeen || !c.already_seen) &&
    state.sizes.has(c.size) &&
    (!state.sector || c.sector === state.sector) &&
    (!state.search || c.ticker.includes(state.search))
  );
}

function sma(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function drawChart(canvas, card, tf) {
  const n = card.closes.length;
  const start = Math.max(0, n - tf);
  const closes = card.closes.slice(start), opens = card.opens.slice(start),
        highs = card.highs.slice(start), lows = card.lows.slice(start);
  if (closes.length < 2) return;

  const sma20 = sma(card.closes, 20).slice(start);
  const sma50 = sma(card.closes, 50).slice(start);

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const allVals = [...highs, ...lows, ...sma20.filter(v=>v!=null), ...sma50.filter(v=>v!=null)];
  const lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.08 || 1;
  const yMin = lo - pad, yMax = hi + pad;
  const y = v => H - ((v - yMin) / (yMax - yMin)) * H;
  const n2 = closes.length;
  const cw = W / n2;
  const x = i => i * cw + cw / 2;

  function line(series, color, dashed) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.setLineDash(dashed ? [3, 3] : []);
    let started = false;
    for (let i = 0; i < series.length; i++) {
      if (series[i] == null) continue;
      const px = x(i), py = y(series[i]);
      if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  line(sma50, isLightTheme() ? 'rgba(61,56,41,0.55)' : 'rgba(201,194,176,0.55)', true);
  line(sma20, isLightTheme() ? 'rgba(22,53,64,0.85)' : 'rgba(143,196,219,0.85)', false);

  // Deliberately more saturated than the --good/--bad text tokens (those
  // are tuned to sit quietly inline in prose; a candlestick needs to read
  // as up/down at a glance, so it gets its own punchier pair here).
  const CANDLE_UP   = isLightTheme() ? '#15803d' : '#4ade80';
  const CANDLE_DOWN = isLightTheme() ? '#b91c1c' : '#f87171';

  for (let i = 0; i < n2; i++) {
    const up = closes[i] >= opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(i), y(highs[i]));
    ctx.lineTo(x(i), y(lows[i]));
    ctx.stroke();
    const bodyTop = y(Math.max(opens[i], closes[i]));
    const bodyBot = y(Math.min(opens[i], closes[i]));
    const bw = Math.max(1, cw * 0.6);
    ctx.fillRect(x(i) - bw / 2, bodyTop, bw, Math.max(1, bodyBot - bodyTop));
  }

  // floating last-price badge
  const lastPrice = closes[closes.length - 1];
  const up = card.change_1d >= 0;
  ctx.font = '600 10px "IBM Plex Mono", monospace';
  const label = lastPrice.toFixed(lastPrice < 1 ? 3 : 2);
  const tw = ctx.measureText(label).width;
  const bx = W - tw - 10, by = y(lastPrice);
  ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
  ctx.fillRect(bx - 4, by - 8, tw + 8, 16);
  ctx.fillStyle = isLightTheme() ? '#fdf8ef' : '#1c1204';
  ctx.fillText(label, bx, by + 3);
}

function cardHtml(c) {
  const chgClass = c.change_1d > 0 ? 'up' : c.change_1d < 0 ? 'dn' : 'neutral';
  const chgSign = c.change_1d > 0 ? '+' : '';
  const statsHtml = c.stats.map(s => `<span>${esc(s.label)} <span class="v">${esc(s.value)}</span></span>`).join('');
  const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${c.ticker}`;
  return `<div class="card${c.already_seen ? ' seen' : ''}" data-ticker="${c.ticker}">
    <div class="cardhead">
      <div class="cardhead-left">
        <span class="ticker"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(c.ticker)} ↗</a></span>
        <span class="chg ${chgClass}">${chgSign}${c.change_1d.toFixed(1)}%</span>
      </div>
      <div class="scorebar"><div class="bg"><div class="fill" style="width:${Math.min(100,c.score)}%"></div></div><span class="num">${c.score}</span></div>
    </div>
    <div class="chartwrap"><canvas></canvas></div>
    <div class="statrow">${statsHtml}</div>
    <div class="sectortag">${esc(c.sector)}</div>
  </div>`;
}

function statNumeric(value) {
  const m = String(value).match(/-?[\d.]+/);
  return m ? parseFloat(m[0]) : NaN;
}

function renderTable(visible) {
  const wrap = document.getElementById('tableWrap');
  if (visible.length === 0) {
    wrap.innerHTML = '<div class="empty table-empty">No stocks match the current filters.</div>';
    return;
  }
  const statLabels = visible[0].stats.map(s => s.label);
  const sorters = {
    ticker: c => c.ticker,
    sector: c => c.sector,
    price: c => c.price,
    change_1d: c => c.change_1d,
    score: c => c.score,
  };
  statLabels.forEach((label, i) => { sorters['stat' + i] = c => statNumeric(c.stats[i].value); });

  const getVal = sorters[state.sortKey] || sorters.score;
  const sorted = [...visible].sort((a, b) => {
    const av = getVal(a), bv = getVal(b);
    if (typeof av === 'string') return state.sortDir * av.localeCompare(bv);
    return state.sortDir * ((av ?? -Infinity) - (bv ?? -Infinity));
  });

  const headers = [
    ['ticker', 'Ticker'], ['sector', 'Sector'], ['price', 'Price'],
    ['change_1d', '1D Chg'], ['score', 'Score'],
    ...statLabels.map((l, i) => ['stat' + i, l]),
  ];
  const thead = headers.map(([key, label]) =>
    `<th class="${state.sortKey === key ? 'sorted' : ''}" data-sortkey="${key}">${esc(label)}${state.sortKey === key ? (state.sortDir === 1 ? ' ↑' : ' ↓') : ''}</th>`
  ).join('');

  const rows = sorted.map(c => {
    const chgClass = c.change_1d > 0 ? 'up' : c.change_1d < 0 ? 'dn' : 'neutral';
    const chgSign = c.change_1d > 0 ? '+' : '';
    const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${c.ticker}`;
    const statCells = c.stats.map(s => `<td>${esc(s.value)}</td>`).join('');
    return `<tr class="${c.already_seen ? 'seen' : ''}">
      <td class="ticker-cell"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(c.ticker)}</a></td>
      <td class="sector-cell">${esc(c.sector)}</td>
      <td>$${c.price.toFixed(c.price < 1 ? 3 : 2)}</td>
      <td class="${chgClass}">${chgSign}${c.change_1d.toFixed(1)}%</td>
      <td>${c.score}</td>
      ${statCells}
    </tr>`;
  }).join('');

  wrap.innerHTML = `<table class="datatable"><thead><tr>${thead}</tr></thead><tbody>${rows}</tbody></table>`;
  wrap.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.sortkey;
    if (state.sortKey === key) state.sortDir *= -1; else { state.sortKey = key; state.sortDir = -1; }
    render();
  }));
}

function render() {
  const visible = filteredCards();
  document.getElementById('countLine').textContent = `${visible.length} stocks`;
  const grid = document.getElementById('grid');
  const tableWrap = document.getElementById('tableWrap');

  if (state.mode === 'table') {
    grid.style.display = 'none';
    tableWrap.style.display = '';
    renderTable(visible);
    return;
  }
  grid.style.display = '';
  tableWrap.style.display = 'none';

  if (visible.length === 0) {
    grid.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  let html = '';
  if (state.view === 'ranked') {
    const sorted = [...visible].sort((a, b) => b.score - a.score);
    html = sorted.map(cardHtml).join('');
  } else {
    const bySector = {};
    visible.forEach(c => { (bySector[c.sector] = bySector[c.sector] || []).push(c); });
    Object.keys(bySector).sort().forEach(sec => {
      const list = bySector[sec].sort((a, b) => b.score - a.score);
      html += `<div class="sectionhead">${esc(sec)} (${list.length})</div>` + list.map(cardHtml).join('');
    });
  }
  grid.innerHTML = html;

  grid.querySelectorAll('.card').forEach(el => {
    const c = DATA.find(d => d.ticker === el.dataset.ticker);
    const canvas = el.querySelector('canvas');
    requestAnimationFrame(() => drawChart(canvas, c, state.tf));
  });
}

render();
</script>
</body>
</html>"""
