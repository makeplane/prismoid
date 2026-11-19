# The frame - the computer the agent works inside (§10).
#
# Everything the toolchain needs is pinned in the image, because an agent that can `pip install`
# is an agent that can reach the network to do it, and the frame's whole premise is that it
# cannot. The only route out is the egress proxy, and the proxy only knows the domains this task
# was allowed.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/work

# curl and ca-certificates. Not a convenience: the egress test IS `curl <domain>`, and a security
# check that passes because the tool is missing is worse than no check at all. (It did. See the
# M1.2 log.) An agent also genuinely needs to fetch an allowlisted URL.
#
# LibreOffice (ADR-023): the rendition engine - docx/pptx → pixel-faithful PDF, xlsx → styled
# HTML - run headless at harvest so the reader shows THE DOCUMENT, not an extraction. The
# industry-standard choice for full-fidelity server-side office rendering, and the same engine
# the reference document skills use for their own visual QA. The metric-compatible font trio
# (Liberation ≈ Arial/Times/Courier, Carlito ≈ Calibri, Caladea ≈ Cambria) is what keeps the
# rendition's line breaks where Word would put them. ~600 MB - the honest cost of fidelity,
# recorded in the ADR; no Java, no GUI stack.
#
# poppler-utils (ADR-034): `pdftoppm` rasterizes the rendition's pages to PNGs so the author
# and the QA gate can LOOK at a deliverable - the visual pass ADR-026 deferred. One small
# static binary set (pdfinfo/pdftotext ride along); PyMuPDF rejected (a second PDF engine,
# 40 MB, AGPL).
#
# fonts-inter (ADR-034): the house face, chosen by eye on rendered pages; installed so every
# PDF rendition embeds it exactly. A .docx/.pptx names Inter and falls back where it is absent.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
      fonts-inter \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# The document/spreadsheet/deck toolchain (§10), pinned. reportlab + pypdf joined for the
# PDF lane (ADR-021): reportlab authors a .pdf the way python-docx authors a .docx, and pypdf
# is how the document suite reads one back to ground its numbers - a format we can write but
# not re-read would ship un-verifiable artifacts. 4.5.x deliberately, not the weeks-old 5.0.
#
# markitdown (ADR-046): the READING half the toolchain never had - `markitdown /task/inputs/x.docx`
# turns a mounted binary into markdown in one command, where agents used to hand-roll a
# python-docx script per read. Format extras only, never [all] (no Azure SDKs, no audio lane
# whose default recognizer is a network call). Its own floors are unpinned, so the majors it
# rides on are pinned here beside it; magika (its type sniffer) drags onnxruntime - the price
# of the door, in an image already carrying LibreOffice.
RUN pip install --no-cache-dir \
      openpyxl==3.1.5 \
      python-docx==1.1.2 \
      python-pptx==1.0.2 \
      pandas==2.2.3 \
      matplotlib==3.9.2 \
      pyyaml==6.0.2 \
      reportlab==4.5.1 \
      pypdf==6.21.4 \
      "markitdown[docx,pptx,xlsx,pdf]==0.1.7" \
      mammoth==1.11.0 \
      "pdfminer.six>=20251230" \
      "pdfplumber>=0.11.9"

# The layout (§10). Created here so it exists even if the driver forgets:
#   /task         the SYSTEM's map - plan.md, criteria.json, answers.json. Read-only to the agent.
#   /task/inputs  the SYSTEM's attachments - the sources a verifier checks against. Read-only.
#   /work         the workshop: clones, notebooks, intermediates. Kept across the task's steps and
#                 turns in its Machine (ADR-063); the checkpoint is `/work` alone.
#   /artifacts    the publish target - the ONLY harvestable directory (Rule 9)
#   /skills       the loader target
#   /verify       the invigilator's desk - a suite runs from here, in a FRESH frame, and the
#                 agent never sees it. Same image, two roles: the sandbox that does the work and
#                 the sandbox that checks it are the same kind of box, which is the point.
RUN mkdir -p /task/inputs /work /artifacts /skills /verify

# The house look (ADR-034): `plane_house` - the one visual identity every docx/pptx/pdf builder
# and the layout checks import. Baked into the image (skills mount only when pinned and cap at
# 256 KiB, so a shared engine cannot live in one). Root-owned, read-only: guidance the system
# gives the agent, never the agent's workshop. The image tag hashes this directory too.
COPY house/ /opt/plane/
ENV PYTHONPATH=/opt/plane
RUN chown -R root:root /opt/plane && chmod -R a-w /opt/plane

# Non-root. An agent with root inside its sandbox is one container escape from being root outside.
#
# **The uid is pinned, and the driver knows it** (`docker_driver.AGENT_UID`). `put_archive` writes
# with the uid recorded in the tar member, so a file the system copies in is owned by whoever the
# tar says - and the tar's default is root. Leave it there and every file we hand the agent is one
# it cannot edit with a shell: an agent writes `/work/build.py`, runs a tool that rewrites it, and
# gets `Permission denied` on its own scratch file. So the driver stamps 1000:1000 for the agent's
# things and 0:0 for the system's, and this line is the other end of that agreement.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/agent --shell /bin/bash agent \
 && chown -R agent:agent /work /artifacts /skills \
 && chmod 0777 /work /artifacts /skills

# **`/task` is the system's, and the agent may only read it.**
#
# An agent that can edit its own sources can launder a hallucination into a fact: write the
# fabricated figure into `/task/inputs/source.md`, and the number-grounding check (M2.1b) finds it
# there and passes. The whole verification story collapses, silently, with a green tick.
#
# So the sources are root-owned and read-only - and **so is `/task` itself**, which is the part
# that is easy to get wrong: making only `/task/inputs` unwritable is not enough. Deleting an entry
# needs write permission on its *containing* directory, so with `/task` at 0777 an agent could
# simply `mv /task/inputs /task/inputs.old && mkdir /task/inputs` and plant whatever it liked. The
# lock has to be on the parent, or it is not a lock.
#
# The system still writes here: `put_archive` extracts as root inside the container, so a 0555
# directory stops the agent and not us. That asymmetry is exactly the point.
#
# `/verify` is root's for the same reason, one step further: a verifier the accused can edit is not
# a verifier. In a fresh frame there is no agent to edit it - but the frame image is one image, and
# a lock that only holds because of who happens to be in the room is not a lock.
RUN chown -R root:root /task /verify && chmod 0555 /task /task/inputs /verify
USER agent
WORKDIR /work

CMD ["sleep", "infinity"]
