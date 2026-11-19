# Go release notes: the week of 2026-11-09

**Bottom line:** two minor releases shipped this week, Go 1.27.4 and Go 1.26.11. Both are security
releases. Upgrade every service that serves HTTP or parses archives.

## What shipped

- **Go 1.27.4** and **Go 1.26.11** are listed in the
  [release history](https://go.dev/doc/devel/release#go1.27.minor), with the fixed packages per
  release.
- The announcement went to the
  [golang-announce list](https://groups.google.com/g/golang-announce/c/q5Lb3GZr9Ck) on Tuesday,
  with a [pre-announcement](https://groups.google.com/g/golang-announce/c/W7xK1dP4m2s/m/Zr8ePq0aBQAJ)
  the Thursday before.
- Binaries and source archives are on the [downloads page](https://go.dev/dl/).

## The security fixes

- **GO-2026-6859** ([report](https://pkg.go.dev/vuln/GO-2026-6859)): net/http could read an
  unbounded header block from a hostile peer. Fixed in both releases.
- **GO-2026-6861** ([report](https://pkg.go.dev/vuln/GO-2026-6861?tab=versions)): archive/zip
  could loop on a crafted directory record. Fixed in both releases.
- The [vulnerability database](https://pkg.go.dev/vuln/) lists both as resolved.

## What to do

Rebuild with Go 1.27.4 (or 1.26.11 on the older line) and redeploy. The
[security policy](https://go.dev/doc/security/policy) explains why minor releases carry no other
changes, so the upgrade is safe to take without a feature review.
