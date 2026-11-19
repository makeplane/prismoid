# The code profile: the base frame plus what a code step's prompt says it has - git, ripgrep,
# Node (with npm) and pytest. Layered on the base tag, so the toolchain is identical; the browser profile is
# layered on this one, so the profiles are a line (base < code < browser) and a wider Machine
# runs every narrower step (`schemas/machines.widen`).
#
# Every version is pinned, and the image tag hashes this file: a bump is a new image on every
# worker. git and ripgrep come from Debian bookworm at one exact build (the ADR-066 rule the
# browser's Chromium follows: a fresh build failing here means the bump to bookworm-security's
# current build is overdue). Node is the official image's own build of the current LTS, by
# version and digest, so its binary is the one nodejs.org verified and signed, for either arch
# (libatomic1 is the one library it links that the base image lacks).
ARG BASE_IMAGE
FROM node:26.13.1-bookworm-slim@sha256:5df79989ed685e09f0a98019db4cebfb22fb9df1a53c0161664a011f7ff6b0f1 AS node

FROM ${BASE_IMAGE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 ripgrep=13.0.0-4+b2 \
      libatomic1=12.2.0-14+deb12u1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# pytest for the base's python, its whole closure pinned (packaging already rides in the base):
# `pytest --junitxml` writes the JUnit report the code receipt counts.
RUN pip install --no-cache-dir pytest==9.1.1 pluggy==1.6.0 iniconfig==2.3.0 pygments==2.20.0

USER agent
WORKDIR /work

CMD ["sleep", "infinity"]
