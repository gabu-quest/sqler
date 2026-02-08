# demo.tape — VHS recording for sqler terminal demo
# Install VHS: go install github.com/charmbracelet/vhs@latest
# Record:      vhs demo.tape

Output docs/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 960
Set Height 600
Set Theme "Catppuccin Mocha"
Set Padding 20

Type "uv run python demo.py" Sleep 500ms Enter

Sleep 12s
