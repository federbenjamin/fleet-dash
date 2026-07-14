-- FleetDashInjector: reads ~/.claude/fleet-dash/inject-request.txt and types the
-- requested keystrokes into the iTerm session owning the requested tty.
-- Request format (plain text lines):
--   line 1: tty device path (/dev/ttysNNN), or the literal SPAWN
--   line 2: request id
--   lines 3+: <flag><space><base64 text>
--     flag 0 = write text raw (no newline) · 1 = write text + newline (LF)
--     flag 2 = press Return: send a raw CR (character id 13) — what raw-mode TUIs
--              actually treat as Enter; iTerm's own newline sends LF
--   SPAWN: creates a NEW tab in the current iTerm window and runs the single
--          line-3 payload (a `cd … && claude …` command the daemon composed from
--          validated inputs). The daemon never passes raw user text here.
-- Writes "<request id> ok" or "<request id> <error>" to inject-result.txt.
on run
	set base to (POSIX path of (path to home folder)) & ".claude/fleet-dash/"
	set resultFile to base & "inject-result.txt"
	try
		set req to do shell script "cat " & quoted form of (base & "inject-request.txt")
		set L to paragraphs of req
		set targetTty to item 1 of L
		set reqId to item 2 of L
	on error errMsg
		do shell script "printf %s " & quoted form of ("read-failed " & errMsg) & " > " & quoted form of resultFile
		return
	end try
	if targetTty is "SPAWN" then
		set outcome to "spawn: no command"
		try
			set ln to item 3 of L
			set cmd to my b64decode(text 3 thru -1 of ln)
			tell application "iTerm2"
				activate
				if (count of windows) is 0 then
					create window with default profile
					tell current session of current window to write text cmd
				else
					tell current window
						set t to (create tab with default profile)
						tell current session of t to write text cmd
					end tell
				end if
			end tell
			set outcome to "ok"
		on error errMsg
			set outcome to errMsg
		end try
		do shell script "printf %s " & quoted form of (reqId & " " & outcome) & " > " & quoted form of resultFile
		return
	end if
	set outcome to "session tty not found in iTerm"
	try
		tell application "iTerm2"
			repeat with w in windows
				repeat with t in tabs of w
					repeat with s in sessions of t
						if tty of s is targetTty then
							repeat with i from 3 to (count of L)
								set ln to item i of L
								set flagChar to ""
								if length of ln > 0 then set flagChar to character 1 of ln
								if flagChar is "2" then
									tell s to write text (character id 13) newline NO
								else if length of ln > 2 then
									set b64 to text 3 thru -1 of ln
									set payload to my b64decode(b64)
									if flagChar is "1" then
										tell s to write text payload newline YES
									else
										tell s to write text payload newline NO
									end if
								else if flagChar is "1" then
									tell s to write text "" newline YES
								end if
								delay 0.4
							end repeat
							set outcome to "ok"
						end if
					end repeat
				end repeat
			end repeat
		end tell
	on error errMsg
		set outcome to errMsg
	end try
	do shell script "printf %s " & quoted form of (reqId & " " & outcome) & " > " & quoted form of resultFile
end run

on b64decode(b64)
	if b64 is "" then return ""
	return do shell script "printf %s " & quoted form of b64 & " | base64 -D"
end b64decode
