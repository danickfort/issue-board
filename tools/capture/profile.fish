set -g fish_greeting
set -g fish_history ''
set -g fish_color_command 8bd5ca
set -g fish_color_param cad3f5
set -g fish_color_autosuggestion 6e738d
function fish_prompt
    set_color 8bd5ca
    printf 'issue-board '
    set_color 6e738d
    printf '› '
    set_color normal
end
function fish_right_prompt
end
