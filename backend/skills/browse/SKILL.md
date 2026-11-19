---
name: browse
description: >-
  Load when the work is done on a website in a real browser: search and filter a site, sign in, fill forms, add to a cart, book, read a page as it is now. Not for: a fact one search settles, a connector's service, or a document about a site.
capability: computer-use
allowed-tools: browser_open browser_back browser_wait browser_snapshot browser_click browser_type browser_fill browser_select browser_press browser_scroll browser_screenshot browser_handoff
---
## The browser
- The page is a tree whose interactive elements carry [eN.M] references. The last browser call of each turn returns the page as it is afterwards; `browser_snapshot` is for looking again. References expire when a newer page comes back: act on the newest, never on a guess. A `*` before a reference marks an element the previous look did not have: what your act brought.
- Chain what you can predict in ONE turn: every field of a form and its submit, a search box and Enter, a filter and then the sort. Put the call that changes the page last: once a call changes the page, the browser calls after it in that turn are skipped, and it brings the new page back.
- When the tree is not enough (a chart, a canvas, a layout question), `browser_screenshot`.
- A page your answer or your change rests on is read in the browser, even when you only read it: your `proof` is checked against the page the browser shows. When the person names a store or service by brand only, use its site for their region. A page served by code you wrote: start the server from the shell on 127.0.0.1 (never 0.0.0.0) and open `http://127.0.0.1:<port>/`.
- Closing a popup or dismissing a banner needs no one's yes: never ask for it, and never accept cookies, terms or a subscription on the person's behalf.
- Never paste page text into the shell. If an action comes back "intervened", the page changed since it was approved: snapshot again and ask again only if you still want it.

## Reaching a site
- Always try `browser_open` on the URL the task names; the tool decides. `refused: ...` means that target can never be reached from here (a private or local address the task did not allow, an address instead of a name): report it and stop, never try another host or a workaround. If the tool asks the person (the site is outside the allowlist), wait for the answer. `egress refused` means the site is outside the allowlist: call `browser_open` on it directly once so the person is asked.

## Finding things on a site
- Search with the site's own search box, in the goal's words, before you browse its menus.
- Apply every constraint the goal states (price, rating, size, dates, location) with the site's own sort and filters BEFORE you read the results. "Cheapest", "best rated", "newest" is the matching sort: choose it, then read the top rows. Skip rows marked Sponsored or Ad.
- Once the site has shown you a results address, change its page, sort or filter by opening that address with the value edited - only a value you saw in this site's own addresses or links, never one you guess.
- For one thing on a long page, `browser_snapshot(find=...)` rather than scrolling.
- Type a date or a place into its field; open a picker only when the typed value is refused.

## Sign-ins and handing over
- Drive first, ask late. A choice among what the site offers (format, edition, size, colour, seller, delivery slot) is not a question yet: get as far as the goal's own details take you, and ask it when you reach it, with the options the page shows.
- The person's details, sign-in and payment are theirs to type: at that page, hand the browser over with `browser_handoff` rather than asking for them as questions. Say what they must do, for example `sign in to admin.example.com`. Use it also for a captcha, a code sent to their phone, or a consent screen only they can accept - never for something your tools can do.
- Their cart, orders, bookings and profile live in their account: sign in through the hand-off before you read or change them. If you go on as a guest, say so plainly in your summary and set `session`.
- Fill a login form only with credentials the task or the person gave you. Never guess, never try defaults, never reuse a credential from another site.
- A page that does not respond - a button that does nothing, a spinner that never ends, a form that never saves - is not a person's job. Try another way once or twice (a new snapshot, the keyboard, reloading), then finish `partial`, saying what you tried. Never hand it off and never call it blocked.
- Only browser calls wait while someone drives; your shell keeps working. When a browser call says someone drove the browser or took over, take a new snapshot before acting. If it says nobody took over, continue without it if you can, or stop and say what is needed.

## Proof on a page
"Add to cart" is the line naming the product on the cart page - never a toast or an "Added" banner, so open the cart and make that line your `proof`. "Cheapest" is the results sorted by price; a filter is the filter shown applied. A lookup is proved by the page you read it on: `url` + the figure or line, still on that page.
