// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// ── The file door, against a played Engine (no socket needed) ──────────────────────────

// An agent's write is the agent's own act inside the sandbox: one exec as 1000:1000 that reads the
// bytes from stdin, never the Engine's archive route as root. The path rides as its own argument,
// never inside the shell text, and the error words are the C locale's.
func TestAgentWriteRunsAsAgentUID(t *testing.T) {
	e := playAttachEngine(t, http.StatusCreated).swallowing(0, "")
	inst := &instance{b: New(e.sock), cid: "c1"}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	data := []byte("bytes\x00\xff 'quoted' $(not run)")
	target := "/work/a b/it's $HOME.txt"
	if err := inst.WriteFile(ctx, target, data, 0o640, core.OwnerAgent); err != nil {
		t.Fatalf("write: %v", err)
	}
	e.mu.Lock()
	created, stdin := e.created, e.stdin
	e.mu.Unlock()
	if !bytes.Equal(stdin, data) {
		t.Fatalf("the bytes go in as the command's stdin, whole: %q", stdin)
	}
	var cmd, env []string
	for _, v := range created["Cmd"].([]any) {
		cmd = append(cmd, v.(string))
	}
	for _, v := range created["Env"].([]any) {
		env = append(env, v.(string))
	}
	if created["User"] != "1000:1000" || created["AttachStdin"] != true || created["Tty"] != false {
		t.Fatalf("an agent write runs as the agent with stdin attached: %v", created)
	}
	if len(cmd) < 4 || cmd[0] != "/bin/sh" || cmd[1] != "-c" || cmd[3] != "sh" {
		t.Fatalf("one fixed script, its arguments after it: %q", cmd)
	}
	if strings.Contains(cmd[2], "/work") || strings.Contains(cmd[2], "it's") {
		t.Fatalf("the path must never be part of the shell text: %q", cmd[2])
	}
	if !slices.Contains(cmd[4:], target) || !slices.Contains(cmd[4:], "/work/a b") || !slices.Contains(cmd[4:], "640") {
		t.Fatalf("the target, its parent and the mode ride as their own arguments: %q", cmd[4:])
	}
	if !slices.Contains(env, "LC_ALL=C") {
		t.Fatalf("the C locale, so a refusal's words are stable: %q", env)
	}
}

// A refusal the sandbox's own permissions make is the caller's forbidden path (403); any other
// failure is a failure, with its reason.
func TestAgentWritePermissionDeniedIsForbidden(t *testing.T) {
	cases := []struct {
		name, stderr string
		exit         int
		forbidden    bool
	}{
		{"cannot create (dash)", "/bin/sh: 4: cannot create /opt/side/.sandbar-1: Permission denied\n", 2, true},
		{"mkdir (busybox)", "mkdir: can't create directory '/task/inputs/new': Permission denied\n", 1, true},
		{"a link into a closed folder", "mkdir: cannot stat '/work/l': Permission denied\n", 1, true},
		{"rename in a sticky folder", "mv: cannot move '/a/.sandbar-1' to '/a/x': Operation not permitted\n", 1, true},
		{"disk full", "cat: write error: No space left on device\n", 1, false},
		{"a short stream", "short write: 3 of 5 bytes\n", 1, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := playAttachEngine(t, http.StatusCreated).swallowing(c.exit, c.stderr)
			inst := &instance{b: New(e.sock), cid: "c1"}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			err := inst.WriteFile(ctx, "/opt/side/code.py", []byte("evil"), 0o644, core.OwnerAgent)
			if err == nil {
				t.Fatal("a failed write must be an error")
			}
			if got := errors.Is(err, core.ErrForbiddenPath); got != c.forbidden {
				t.Fatalf("errors.Is(ErrForbiddenPath) = %v, want %v (err: %v)", got, c.forbidden, err)
			}
			if !strings.Contains(err.Error(), strings.TrimSpace(c.stderr)) {
				t.Fatalf("the error carries the command's reason: %v", err)
			}
		})
	}
}

// A system write is the Engine's, which follows links in the directory it writes into. So root
// first looks at every component of the parent without following any: a link the workload planted
// is refused before anything is made or written, and a component root cannot even see is refused
// too - it cannot be checked.
func TestSystemWriteRefusesSymlinkedParent(t *testing.T) {
	inst, creates := execEngine{createCode: 201, stdout: "directory\nsymbolic link\n"}.play()
	err := inst.WriteFile(context.Background(), "/skills/s/SKILL.md", []byte("x"), 0o444, core.OwnerSystem)
	if !errors.Is(err, core.ErrForbiddenPath) || !strings.Contains(err.Error(), "/skills/s") {
		t.Fatalf("a symlinked parent is forbidden, naming the link: %v", err)
	}
	c := creates()
	want := []string{"stat", "-c", "%F", "--", "/skills", "/skills/s"}
	if len(c) != 1 || c[0].User != "0:0" || !slices.Equal(c[0].Cmd, want) {
		t.Fatalf("one look as root, every component its own argument, nothing made: %+v", c)
	}
	if !slices.Contains(c[0].Env, "LC_ALL=C") || !slices.Contains(c[0].Env, "HOME=/root") {
		t.Fatalf("root runs in the C locale with its own HOME: %q", c[0].Env)
	}

	inst, creates = execEngine{createCode: 201, exit: 1, stderr: "stat: cannot statx '/browser/profile': Permission denied\n"}.play()
	err = inst.WriteFile(context.Background(), "/browser/profile/x", []byte("x"), 0o444, core.OwnerSystem)
	if !errors.Is(err, core.ErrForbiddenPath) || len(creates()) != 1 {
		t.Fatalf("a parent root cannot look at is forbidden, before anything is made: %v (%d execs)", err, len(creates()))
	}
}

// A parent that is all directories is written into as it is: one look, no mkdir, then the Engine's
// write. A parent not all there yet is made, as root, before the write.
func TestSystemWriteMakesOnlyAMissingParent(t *testing.T) {
	inst, creates := execEngine{createCode: 201, stdout: "directory\ndirectory\n"}.play()
	if err := inst.WriteFile(context.Background(), "/task/inputs/a.csv", []byte("a,b"), 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("write: %v", err)
	}
	if c := creates(); len(c) != 1 || c[0].Cmd[0] != "stat" {
		t.Fatalf("a parent already there is not made again: %+v", c)
	}

	inst, creates = execEngine{createCode: 201, exit: 1, stdout: "directory\n", stderr: "stat: '/task/inputs': No such file or directory\n",
		then: []execEngine{{}}}.play()
	if err := inst.WriteFile(context.Background(), "/task/inputs/a.csv", []byte("a,b"), 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("write: %v", err)
	}
	c := creates()
	if len(c) != 2 || c[1].User != "0" || !slices.Equal(c[1].Cmd, []string{"mkdir", "-p", "/task/inputs"}) {
		t.Fatalf("the missing parent is made as root: %+v", c)
	}

	inst, creates = execEngine{createCode: 201, exit: 1, stderr: "stat: can't stat '/new': No such file or directory\n",
		then: []execEngine{{}}}.play()
	if err := inst.WriteFile(context.Background(), "/new/a.csv", []byte("a,b"), 0o444, core.OwnerSystem); err != nil || len(creates()) != 2 {
		t.Fatalf("a parent none of which is there is made: %v (%d execs)", err, len(creates()))
	}

	inst, creates = execEngine{createCode: 201}.play()
	if err := inst.WriteFile(context.Background(), "/top.txt", []byte("x"), 0o444, core.OwnerSystem); err != nil || len(creates()) != 0 {
		t.Fatalf("a file at the root has no parent to look at: %v (%d execs)", err, len(creates()))
	}
}

// execAs keeps at most limit bytes of a command's stdout; one more ends the stream and is an error,
// so a caller that asked for a bound never holds more than it.
func TestExecAsStopsAtItsLimit(t *testing.T) {
	inst, creates := execEngine{createCode: 201, exit: 3, stdout: "hello world", stderr: "why"}.play()
	var out bytes.Buffer
	exit, stderr, err := inst.execAs(context.Background(), core.AgentUID, []string{"cat", "--", "/work/x"}, nil, &out, 64)
	if err != nil || exit != 3 || stderr != "why" || out.String() != "hello world" {
		t.Fatalf("under the limit: exit %d, stderr %q, out %q, %v", exit, stderr, out.String(), err)
	}
	if c := creates(); len(c) != 1 || c[0].User != "1000:1000" || !slices.Equal(c[0].Cmd, []string{"cat", "--", "/work/x"}) {
		t.Fatalf("the argv as given, as uid:gid: %+v", c)
	}
	out.Reset()
	_, _, err = inst.execAs(context.Background(), core.AgentUID, []string{"cat", "--", "/work/x"}, nil, &out, 5)
	if !errors.Is(err, errOverLimit) || out.String() != "hello" {
		t.Fatalf("past the limit: out %q, %v", out.String(), err)
	}
}

// A stdin that breaks mid-stream is the caller's error, and never a hang: the connection is closed,
// so the command sees its input end (a file write then refuses the short file) instead of waiting
// for bytes that will not come.
func TestExecAsEndsTheInputOfASourceThatBroke(t *testing.T) {
	e := playAttachEngine(t, http.StatusCreated).swallowing(1, "short write: 4 of 9 bytes")
	inst := &instance{b: New(e.sock), cid: "c1"}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	broke := errors.New("the source broke")
	src := io.MultiReader(strings.NewReader("part"), failingReader{broke})
	_, _, err := inst.execAs(ctx, core.AgentUID, []string{"cat"}, src, io.Discard, 0)
	if !errors.Is(err, broke) || ctx.Err() != nil {
		t.Fatalf("want the source's error before the deadline, got %v (ctx %v)", err, ctx.Err())
	}
}

// ── A read is the agent's too ───────────────────────────────────────────────────────────────

// A read runs inside the sandbox as the agent, never through the Engine's archive route as root:
// one exec as 1000:1000 of one fixed script, its arguments the path and the bound, never shell
// text. The script looks (stat, following a link), then reads with head exactly the size it saw;
// the error words are the C locale's, and the bytes come back exactly.
func TestReadRunsAsAgentUID(t *testing.T) {
	data := "bytes\x00\xff 'quoted' $(not run)"
	inst, creates := execEngine{createCode: 201, stdout: fmt.Sprintf("%d\n%s", len(data), data)}.play()
	target := "/work/a b/it's $HOME.txt"
	got, err := inst.ReadFile(context.Background(), target)
	if err != nil || string(got) != data {
		t.Fatalf("read: %q %v", got, err)
	}
	c := creates()
	want := []string{"/bin/sh", "-c", agentReadScript, "sh", target, "536870912"}
	if len(c) != 1 || c[0].User != "1000:1000" || !slices.Equal(c[0].Cmd, want) {
		t.Fatalf("one exec of the fixed script, as the agent: %+v", c)
	}
	if strings.Contains(agentReadScript, "/work") || !strings.Contains(agentReadScript, `stat -L -c '%s %F' -- "$1"`) ||
		!strings.Contains(agentReadScript, `exec head -c "$n" -- "$1"`) {
		t.Fatalf("a look, then exactly the size it saw, the path after --: %s", agentReadScript)
	}
	if !slices.Contains(c[0].Env, "LC_ALL=C") || !slices.Contains(c[0].Env, systemPATH) {
		t.Fatalf("the C locale and the system's folders: %q", c[0].Env)
	}
	if _, err := inst.ReadFile(context.Background(), "/work/../browser/x"); !errors.Is(err, core.ErrBadSpec) || len(creates()) != 1 {
		t.Fatalf("an unclean path never reaches the sandbox: %v", err)
	}
}

// The size comes first, on a line of its own, and the bytes land in one buffer made for it,
// however the stream is cut; a byte past the size ends the stream. A first line that is no size is
// let by unkept, for the command's exit to say why.
func TestSizedReadHoldsTheFileOnce(t *testing.T) {
	var r sizedRead
	for _, part := range []string{"1", "1\nhel", "lo", " world"} {
		if _, err := r.Write([]byte(part)); err != nil {
			t.Fatalf("write %q: %v", part, err)
		}
	}
	if !r.sized || string(r.data) != "hello world" || cap(r.data) != 11 {
		t.Fatalf("one buffer of the size: %q (cap %d)", r.data, cap(r.data))
	}
	if _, err := r.Write([]byte("!")); err == nil {
		t.Fatal("a byte past the size must end the stream")
	}
	for _, first := range []string{"OCI runtime exec failed: exec failed: unable to start\r\n", "-1\n", "536870913\n", "12x\n"} {
		var r sizedRead
		if n, err := r.Write([]byte(first + "more")); err != nil || n != len(first)+4 || r.sized {
			t.Fatalf("%q: let by unkept, got %d %v (sized %v)", first, n, err, r.sized)
		}
	}
}

// readEngine plays one read: its exit, stdout and stderr.
func readEngine(createCode, exit int, stdout, stderr string) *instance {
	inst, _ := execEngine{createCode: createCode, exit: exit, stdout: stdout, stderr: stderr}.play()
	return inst
}

// A path that is not there - absent, under a parent that is a file, a link that leads nowhere, or
// gone between the look and the read - is the caller's no-such-path (404), in busybox's words and
// coreutils'. A vanished sandbox and any other failure stay failures. The defect this pins: a
// model's read of an absent /task/context.md came back a 502, the driver raised, and the whole
// step died.
func TestReadMissingIsNoSuchPath(t *testing.T) {
	cases := []struct {
		name, stderr string
		missing      bool
	}{
		{"absent (busybox)", "stat: can't stat '/task/context.md': No such file or directory\n", true},
		{"absent (coreutils)", "stat: cannot statx '/task/context.md': No such file or directory\n", true},
		{"a parent is a file", "stat: cannot statx '/etc/hostname/x': Not a directory\n", true},
		{"a link loop (busybox)", "stat: can't stat '/work/l': Symbolic link loop\n", true},
		{"a link loop (coreutils)", "stat: cannot statx '/work/l': Too many levels of symbolic links\n", true},
		{"gone before the read (busybox)", "head: /task/context.md: No such file or directory\n", true},
		{"gone before the read (coreutils)", "head: cannot open '/task/context.md' for reading: No such file or directory\n", true},
		{"an I/O failure", "head: /task/context.md: I/O error\n", false},
		{"a name that says it", "stat: can't stat '/work/No such file or directory/x': Permission denied\n", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := readEngine(201, 1, "", c.stderr).ReadFile(context.Background(), "/task/context.md")
			if err == nil || errors.Is(err, core.ErrNoSuchPath) != c.missing {
				t.Fatalf("errors.Is(ErrNoSuchPath) want %v, got %v", c.missing, err)
			}
		})
	}
	if _, err := readEngine(http.StatusNotFound, 0, "", "").ReadFile(context.Background(), "/task/context.md"); err == nil || errors.Is(err, core.ErrNoSuchPath) {
		t.Fatalf("a vanished sandbox is a failure, not a missing path: %v", err)
	}
}

// What holds no regular file is no file to read: a directory, a pipe that would never end, a
// device. The script refuses it before anything opens it, and head's word for a directory that
// appeared since is the same answer.
func TestReadDirectoryIsNoSuchPath(t *testing.T) {
	for _, stderr := range []string{
		"/work/x is a directory, not a regular file\n",
		"/work/x is a fifo, not a regular file\n",
		"/work/x is a character special file, not a regular file\n",
		"head: error reading '/work/x': Is a directory\n",
	} {
		_, err := readEngine(201, 1, "", stderr).ReadFile(context.Background(), "/work/x")
		if !errors.Is(err, core.ErrNoSuchPath) || !strings.Contains(err.Error(), "/work/x") {
			t.Fatalf("%q: want ErrNoSuchPath naming the path, got %v", stderr, err)
		}
	}
}

// What the sandbox's permissions keep from the agent - the browser user's closed profile, a file
// only root may read, a link into either - is the caller's forbidden path (403), with the reason:
// refused at the look (a folder the agent cannot enter) or at the read (a file it cannot open).
func TestReadPermissionDeniedIsForbidden(t *testing.T) {
	for _, stderr := range []string{
		"stat: can't stat '/browser/profile/Cookies': Permission denied\n",
		"stat: cannot statx '/work/l/Cookies': Permission denied\n",
		"head: /etc/shadow: Permission denied\n",
		"head: cannot open '/etc/shadow' for reading: Permission denied\n",
	} {
		_, err := readEngine(201, 1, "", stderr).ReadFile(context.Background(), "/browser/profile/Cookies")
		if !errors.Is(err, core.ErrForbiddenPath) || errors.Is(err, core.ErrNoSuchPath) || !strings.Contains(err.Error(), "Permission denied") {
			t.Fatalf("%q: want ErrForbiddenPath with its reason, got %v", stderr, err)
		}
	}
}

// A file over the bound is refused at the look, in the words a read always used, and none of it is
// read: a failure (502), neither a missing nor a forbidden path.
func TestReadOverTheBoundIsRefusedUpFront(t *testing.T) {
	msg := "/work/big is 629145600 bytes, over the 536870912-byte bound on a single file"
	_, err := readEngine(201, readOverBound, "", msg+"\n").ReadFile(context.Background(), "/work/big")
	if err == nil || err.Error() != msg || errors.Is(err, core.ErrNoSuchPath) || errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("want %q, got %v", msg, err)
	}
}

// The read holds no more than the size the look saw: a stream past it is a failure, never a file,
// and a read that printed no size is one too.
func TestReadTakesNoMoreThanTheLookSaw(t *testing.T) {
	if _, err := readEngine(201, 0, "5\nhello world", "").ReadFile(context.Background(), "/work/x"); err == nil {
		t.Fatal("more than the size the look saw must be a failure")
	}
	if got, err := readEngine(201, 0, "5\nhel", "").ReadFile(context.Background(), "/work/x"); err != nil || string(got) != "hel" {
		t.Fatalf("a file that shrank since is what it holds now: %q %v", got, err)
	}
	if got, err := readEngine(201, 0, "0\n", "").ReadFile(context.Background(), "/work/empty"); err != nil || got == nil || len(got) != 0 {
		t.Fatalf("an empty file is empty bytes: %q %v", got, err)
	}
	if _, err := readEngine(201, 0, "", "").ReadFile(context.Background(), "/work/x"); err == nil {
		t.Fatal("a read that printed no size must be a failure")
	}
}

// The Engine says why it could not start a command on stdout (exit 126 or 127), not stderr. That
// reason is kept: past a limit of none it is in the error, and from a command that printed no
// reason of its own it is the reason - never "output over the limit" alone, never an empty one.
func TestExecAsSaysWhyACommandCouldNotStart(t *testing.T) {
	oci := "OCI runtime exec failed: exec failed: unable to start container process: exec: \"/bin/sh\": stat /bin/sh: no such file or directory: unknown\r\n"
	inst, _ := execEngine{createCode: 201, exit: 127, stdout: oci}.play()
	_, _, err := inst.execAs(context.Background(), core.AgentUID, []string{"/bin/sh"}, nil, io.Discard, 0)
	if !errors.Is(err, errOverLimit) || !strings.Contains(err.Error(), "OCI runtime exec failed") {
		t.Fatalf("a limit of none: want the Engine's reason in the error, got %v", err)
	}
	exit, stderr, err := inst.execAs(context.Background(), core.AgentUID, []string{"/bin/sh"}, nil, io.Discard, 1<<10)
	if err != nil || exit != 127 || !strings.Contains(stderr, "OCI runtime exec failed") {
		t.Fatalf("under the limit: want exit 127 with the Engine's reason, got %d %q %v", exit, stderr, err)
	}
	if err := inst.WriteFile(context.Background(), "/work/x", []byte("x"), 0o644, core.OwnerSystem); err == nil || !strings.Contains(err.Error(), "OCI runtime exec failed") {
		t.Fatalf("a system write whose look cannot start says why: %v", err)
	}
	if _, err := inst.ReadFile(context.Background(), "/work/x"); err == nil || errors.Is(err, core.ErrNoSuchPath) || !strings.Contains(err.Error(), "OCI runtime exec failed") {
		t.Fatalf("a read that cannot start is a failure that says why, never the file: %v", err)
	}

}

// Every command root runs finds it in the image's own folders, never in one the image's PATH adds
// that the workload could write: the look before a system write, the mkdir after it, the removal.
func TestRootFindsItsCommandsOnlyInTheSystemFolders(t *testing.T) {
	inst, creates := execEngine{createCode: 201, exit: 1, stdout: "directory\n", stderr: "stat: '/task/inputs': No such file or directory\n",
		then: []execEngine{{}}}.play()
	if err := inst.WriteFile(context.Background(), "/task/inputs/a.csv", []byte("a,b"), 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := inst.Remove(context.Background(), []string{"/task/inputs"}); err != nil {
		t.Fatalf("remove: %v", err)
	}
	c := creates()
	if len(c) != 3 {
		t.Fatalf("look, mkdir, remove: %+v", c)
	}
	for _, ex := range c {
		if !strings.HasPrefix(ex.User, "0") || !slices.Contains(ex.Env, systemPATH) || !slices.Contains(ex.Env, "HOME=/root") {
			t.Fatalf("root, root's HOME and the system folders only: %+v", ex)
		}
	}
	if systemPATH != "PATH=/usr/sbin:/usr/bin:/sbin:/bin" {
		t.Fatalf("the system folders: %s", systemPATH)
	}
}

// The look before a system write fails closed. A stat the Engine could not start (its reason on
// stdout, exit 127) is refused, never read as components that are not there - which would make the
// parent and write through a link it never looked at. So is a line that names no kind of file, a
// count of lines that does not add up, and a reason that is not only missing components.
func TestSystemWriteLookFailsClosed(t *testing.T) {
	cannot := "OCI runtime exec failed: exec failed: unable to start container process: exec: \"stat\": executable file not found in $PATH: unknown\r\n"
	cases := []struct {
		name, path string
		look       execEngine
		why        string
	}{
		{"stat cannot start", "/a/b/c/d/e/f/x.md", execEngine{exit: 127, stdout: cannot}, "executable file not found"},
		{"no kind of file", "/task/x/f.md", execEngine{stdout: "directory\nweird thing\n"}, "weird thing"},
		{"a line short", "/task/x/f.md", execEngine{stdout: "directory\n"}, "accounted for 1 of its 2"},
		{"a line over", "/task/f.md", execEngine{stdout: "directory\ndirectory\n"}, "accounted for 2 of its 1"},
		{"no reason given", "/task/x/f.md", execEngine{exit: 1, stdout: "directory\n"}, "exit 1"},
		{"not only missing", "/task/x.txt/y/f.md", execEngine{exit: 1, stdout: "directory\nregular file\n",
			stderr: "stat: cannot statx '/task/x.txt/y': Not a directory\n"}, "Not a directory"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			c.look.createCode = 201
			inst, creates := c.look.play()
			err := inst.WriteFile(context.Background(), c.path, []byte("x"), 0o444, core.OwnerSystem)
			if err == nil || !strings.Contains(err.Error(), c.why) {
				t.Fatalf("want a refusal naming %q, got %v", c.why, err)
			}
			if n := len(creates()); n != 1 {
				t.Fatalf("refused at the look: nothing made, nothing written (%d execs)", n)
			}
		})
	}
}

// A parent root could not make is the write's failure, with mkdir's reason: the Engine's write is
// never aimed at a directory that is not there.
func TestSystemWriteRefusesAParentItCouldNotMake(t *testing.T) {
	inst, creates := execEngine{createCode: 201, exit: 1, stdout: "directory\n", stderr: "stat: '/task/inputs': No such file or directory\n",
		then: []execEngine{{exit: 1, stderr: "mkdir: can't create directory '/task/inputs': Read-only file system\n"}}}.play()
	err := inst.WriteFile(context.Background(), "/task/inputs/a.csv", []byte("a,b"), 0o444, core.OwnerSystem)
	if err == nil || !strings.Contains(err.Error(), "Read-only file system") || len(creates()) != 2 {
		t.Fatalf("want mkdir's refusal before any write, got %v (%d execs)", err, len(creates()))
	}
}

// What a command printed is its reason only when the Engine could not start it (exit 126 or 127).
// A read that died otherwise (here by a signal, exit 137) never has the file's bytes for a reason:
// a file that ends in "No such file or directory" is not a missing path.
func TestAReadThatDiedKeepsItsBytesOutOfTheReason(t *testing.T) {
	body := "No such file or directory"
	_, err := readEngine(201, 137, fmt.Sprintf("%d\n%s", len(body), body), "").ReadFile(context.Background(), "/work/x")
	if err == nil || errors.Is(err, core.ErrNoSuchPath) || strings.Contains(err.Error(), body) || !strings.Contains(err.Error(), "exit 137") {
		t.Fatalf("want a failure naming the exit, without the file's bytes, got %v", err)
	}
}
