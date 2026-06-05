param(
    [string]$ProjectPath = "D:\dev\workspace\AI\ssh-mcp"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "Claude Code CLI was not found in PATH. Install or open the shell where 'claude' is available."
}

$secure = Read-Host "Private key passphrase" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)

try {
    $passphrase = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $env:SSH_MCP_PROJECT = $ProjectPath
    $env:SSH_MCP_KEY_PASSPHRASE = $passphrase
    Set-Location -LiteralPath $ProjectPath
    claude
}
finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Item Env:SSH_MCP_KEY_PASSPHRASE -ErrorAction SilentlyContinue
}
