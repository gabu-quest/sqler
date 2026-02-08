# demo.tape — VHS recording for sqler terminal demo
# Install VHS: go install github.com/charmbracelet/vhs@latest
# Record:      vhs demo.tape

Output docs/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 960
Set Height 640
Set Theme "Catppuccin Mocha"
Set Padding 20
Set TypingSpeed 50ms

Type "uv run python demo.py"
Sleep 500ms
Enter
Sleep 10s
