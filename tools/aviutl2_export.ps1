<#
AviUtl2 に .aup2 を連番の PNG で書き出させる 画面の操作の本体（tools/aviutl2_export.py から呼ぶ）

手で走らせず、入口の aviutl2_export.py を使う 入口が AviUtl2 と YMM4 が開いていないかを
確かめ、設定のファイルの控えを取り、終わったあとに戻して確かめる

AviUtl2 v2.1.6a で確かめた手順（Issue #108）
  1. aviutl2.exe "<.aup2>" で起動し、主の窓（aviutl2Manager）の題名がプロジェクトの名前に
     なるまで待つ
  2. 主の窓のメニュー〔ファイル〕→〔ファイル出力〕→〔連番ファイル出力〕の番号を名前で引き、
     WM_COMMAND を送る AviUtl2 のメニューは Win32 の普通のメニューで、UI Automation には
     出てこない 番号はプラグインの並びで変わるので決め打ちしない
  3. 「連番ファイル出力」の窓（古い形の保存の窓）の名前の欄へ WM_SETTEXT で書き、〔保存〕を押す
  4. 決まった枚数の PNG が揃い、最後の 1 枚を AviUtl2 が手放すまで待つ
  5. 主の窓へ WM_CLOSE を送り、閉じるのを待つ

書き出しの設定（PNG の透明色・JPEG の品質）は変えない 窓に出ている設定をそのまま言うだけ
名前は入口が決めた一時のフォルダの中の frame.png AviUtl2 はここへ frame000.png から番号を振る

Windows PowerShell 5.1 の罠（ymm4_export.ps1 と同じ）
  - BOM の無い .ps1 は Shift_JIS として読まれ、日本語の名前が全部化ける BOM 付きの UTF-8 で置く
  - UI Automation の型は Add-Type のあとで文字列から引く
  - 変数名は大文字小文字を区別しない 1 文字の名前を使わない
  - 標準出力の日本語は Say で UTF-8 のまま書く

終了コード 0 書き出せた / 1 書き出せなかった
#>
param(
    [Parameter(Mandatory = $true)][string]$Aviutl2,
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Folder,
    [Parameter(Mandatory = $true)][int]$Frames,
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'

$script:StandardOutput = [Console]::OpenStandardOutput()

function Say([string]$Text) {
    # Write-Host は窓の無いときに Shift_JIS で書く 入口はパイプで受けるので UTF-8 のまま渡す
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text + "`n")
    $script:StandardOutput.Write($bytes, 0, $bytes.Length)
    $script:StandardOutput.Flush()
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace SashimonoAviutl2 -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "SendMessageW", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr SendText(System.IntPtr hWnd, int msg, System.IntPtr wParam, string lParam);
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "SendMessageW", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr ReadText(System.IntPtr hWnd, int msg, System.IntPtr wParam, System.Text.StringBuilder lParam);
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "PostMessageW")]
public static extern bool Post(System.IntPtr hWnd, int msg, System.IntPtr wParam, System.IntPtr lParam);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetMenu(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern int GetMenuItemCount(System.IntPtr hMenu);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetSubMenu(System.IntPtr hMenu, int nPos);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern uint GetMenuItemID(System.IntPtr hMenu, int nPos);
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern int GetMenuStringW(System.IntPtr hMenu, uint uIDItem, System.Text.StringBuilder lpString, int cchMax, uint flags);
'@

# 型は Add-Type のあとで引く（読む時点で解決させると見つからない）
$Uia = 'System.Windows.Automation.AutomationElement' -as [type]
$Scope = 'System.Windows.Automation.TreeScope' -as [type]
$NativeApi = 'SashimonoAviutl2.Native' -as [type]

$WM_SETTEXT = 0x000C
# 読み返しは WM_GETTEXT で送る GetWindowText は別の process の Edit の中身を返さない
$WM_GETTEXT = 0x000D
$WM_CLOSE = 0x0010
$WM_COMMAND = 0x0111
$BM_CLICK = 0x00F5
$MF_BYPOSITION = 0x400

$ProcessName = [System.IO.Path]::GetFileNameWithoutExtension($Aviutl2)
$ProjectName = [System.IO.Path]::GetFileName($Project)
$MainClass = 'aviutl2Manager'
$MenuPath = @('ファイル', 'ファイル出力', '連番ファイル出力')
$DialogName = '連番ファイル出力'
# 保存の窓の名前の欄（ComboBoxEx の中の Edit）と〔保存〕〔キャンセル〕 古い形の保存の窓の決まった番号
$NameBoxId = '1148'
$SaveButtonId = '1'
$CancelButtonId = '2'
# 窓の下に出る書き出しの設定の欄
$SceneInfoId = '1003'
$OutputInfoId = '1004'
$BaseName = 'frame'

# 起動とプロジェクトの読み込み プラグインが多いと起動に時間がかかる
$LoadSeconds = 180
$DialogSeconds = 30
# 最後の 1 枚の大きさがこの秒数止まり、手放されたら書き終えたとみなす
$SettleSeconds = 3

function New-Condition($Property, $Value) {
    return New-Object System.Windows.Automation.PropertyCondition($Property, $Value)
}

function New-AndCondition($First, $Second) {
    return New-Object System.Windows.Automation.AndCondition($First, $Second)
}

function Wait-For([scriptblock]$Probe, [int]$Seconds, [string]$What) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        $found = $null
        # 作り直しの途中の要素を触ると例外になる 次の周で拾い直せばよい
        try { $found = & $Probe } catch { $found = $null }
        if ($found) { return $found }
        Start-Sleep -Milliseconds 500
    }
    throw "$What が $Seconds 秒待っても見つからない"
}

# この道具が起動した AviUtl2 動かしている間に本人が別の AviUtl2 を開いても、そちらは触らない
$script:Aviutl2Process = $null

function Get-TopWindows {
    if (-not $script:Aviutl2Process) { return @() }
    return @($Uia::RootElement.FindAll($Scope::Children, (New-Condition $Uia::ProcessIdProperty $script:Aviutl2Process.Id)))
}

function Test-Exited {
    $script:Aviutl2Process.Refresh()
    return $script:Aviutl2Process.HasExited
}

function Wait-MainWindow {
    $until = (Get-Date).AddSeconds($LoadSeconds)
    while ((Get-Date) -lt $until) {
        if (Test-Exited) { throw "起動した AviUtl2（process $($script:Aviutl2Process.Id)）が主の窓を出さずに終わった" }
        foreach ($window in Get-TopWindows) {
            try {
                # 題名がプロジェクトの名前になれば読み込み終わり 起動の途中は題名が違う
                if ($window.Current.ClassName -eq $MainClass -and $window.Current.Name -eq $ProjectName) { return $window }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "AviUtl2 の題名が「$ProjectName」になるのを $LoadSeconds 秒待ったがならない"
}

function Get-MenuText($Menu, [int]$Position) {
    $buffer = New-Object System.Text.StringBuilder 512
    [void]$NativeApi::GetMenuStringW($Menu, [uint32]$Position, $buffer, $buffer.Capacity, $MF_BYPOSITION)
    # 「プロジェクトを保存<タブ>Ctrl+S」のように、タブの後ろはショートカットの表示
    return $buffer.ToString().Split("`t")[0]
}

function Find-MenuCommand($Main) {
    $menu = $NativeApi::GetMenu([System.IntPtr]$Main.Current.NativeWindowHandle)
    if ($menu -eq [System.IntPtr]::Zero) { throw '主の窓にメニューが無い' }
    for ($depth = 0; $depth -lt $MenuPath.Count; $depth++) {
        $wanted = $MenuPath[$depth]
        $found = $false
        $count = $NativeApi::GetMenuItemCount($menu)
        for ($position = 0; $position -lt $count; $position++) {
            if ((Get-MenuText $menu $position) -ne $wanted) { continue }
            if ($depth -eq $MenuPath.Count - 1) { return [int]$NativeApi::GetMenuItemID($menu, $position) }
            $menu = $NativeApi::GetSubMenu($menu, $position)
            $found = $true
            break
        }
        if (-not $found) { throw "メニューに〔$wanted〕が見つからない" }
    }
    throw 'メニューをたどれなかった'
}

function Get-Dialog($Main) {
    return $Main.FindFirst($Scope::Children, (New-AndCondition (New-Condition $Uia::NameProperty $DialogName) (New-Condition $Uia::ClassNameProperty '#32770')))
}

function Find-ById($Root, [string]$Id, [string]$Class) {
    return $Root.FindFirst($Scope::Descendants, (New-AndCondition (New-Condition $Uia::AutomationIdProperty $Id) (New-Condition $Uia::ClassNameProperty $Class)))
}

function Save-Sequence($Main, [string]$Name) {
    $dialog = Wait-For { Get-Dialog $Main } $DialogSeconds "「$DialogName」の窓"
    foreach ($id in @($SceneInfoId, $OutputInfoId)) {
        $info = Find-ById $dialog $id 'Static'
        if ($info) { Say "書き出しの設定 $($info.Current.Name)" }
    }
    $nameBox = Wait-For { Find-ById $dialog $NameBoxId 'Edit' } $DialogSeconds '保存の名前の欄'
    $handle = [System.IntPtr]$nameBox.Current.NativeWindowHandle
    [void]$NativeApi::SendText($handle, $WM_SETTEXT, [System.IntPtr]::Zero, $Name)
    $buffer = New-Object System.Text.StringBuilder 4096
    [void]$NativeApi::ReadText($handle, $WM_GETTEXT, [System.IntPtr]$buffer.Capacity, $buffer)
    if ($buffer.ToString() -ne $Name) {
        # 違う名前のまま保存すると、別の所へ書き出して「見つからない」で終わる
        throw "保存の名前の欄に書けない（読み返すと「$($buffer.ToString())」）"
    }
    $save = Find-ById $dialog $SaveButtonId 'Button'
    if (-not $save) { throw '〔保存〕が見つからない' }
    # 送って返りを待たない 書き出しはこの押下の中で進むので、SendMessage だと書き終えるまで返らない
    [void]$NativeApi::Post([System.IntPtr]$save.Current.NativeWindowHandle, $BM_CLICK, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
}

function Close-Dialog($Main) {
    # 失敗の途中で保存の窓が残っていると、主の窓が閉じない 取り消して閉じる
    $dialog = Get-Dialog $Main
    if (-not $dialog) { return }
    $cancel = Find-ById $dialog $CancelButtonId 'Button'
    if ($cancel) {
        [void]$NativeApi::Post([System.IntPtr]$cancel.Current.NativeWindowHandle, $BM_CLICK, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
        Start-Sleep -Seconds 1
    }
}

function Get-Pictures {
    return @(Get-ChildItem -LiteralPath $Folder -Filter "$BaseName*.png" -File -ErrorAction SilentlyContinue)
}

function Test-Released([string]$Path) {
    # 書き手が開いたままなら、共有なしでは開けない
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'Read', 'None')
        $stream.Dispose()
        return $true
    } catch {
        return $false
    }
}

function Wait-Written {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    [void](Wait-For { (Get-Pictures).Count -gt 0 } $DialogSeconds '書き出しの始まり（最初の PNG）')
    Say '書き出しています'
    $lastSize = -1
    $stableSince = $null
    while ($true) {
        if ((Get-Date) -gt $deadline) { throw "書き出しが $TimeoutSeconds 秒で終わらない（$((Get-Pictures).Count) / $Frames 枚）" }
        if (Test-Exited) { throw "書き出しの途中で AviUtl2 が終わった（$((Get-Pictures).Count) / $Frames 枚）" }
        $pictures = Get-Pictures
        if ($pictures.Count -gt $Frames) {
            # 多いまま待っても揃わない 入口の数え方とプロジェクトが食い違っている
            throw "PNG が $($pictures.Count) 枚あり、見込みの $Frames 枚より多い"
        }
        if ($pictures.Count -eq $Frames) {
            $last = $pictures | Sort-Object Name | Select-Object -Last 1
            $size = $last.Length
            if ($size -gt 0 -and $size -eq $lastSize) {
                if (-not $stableSince) { $stableSince = Get-Date }
                if (((Get-Date) - $stableSince).TotalSeconds -ge $SettleSeconds -and (Test-Released $last.FullName)) { return }
            } else {
                $stableSince = $null
            }
            $lastSize = $size
        }
        Start-Sleep -Milliseconds 500
    }
}

function Close-Aviutl2 {
    foreach ($window in Get-TopWindows) {
        try {
            if ($window.Current.ClassName -eq $MainClass) {
                [void]$NativeApi::Post([System.IntPtr]$window.Current.NativeWindowHandle, $WM_CLOSE, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
            }
        } catch { }
    }
    $until = (Get-Date).AddSeconds($DialogSeconds)
    while ((Get-Date) -lt $until) {
        if (Test-Exited) { return }
        Start-Sleep -Milliseconds 500
    }
    # 無理に止めない 止めると AviUtl2 が設定を書き終える前に終わり、入口が戻した設定の上に
    # 途中まで書いた物が残りかねない 保存を尋ねる窓などが出ていないか本人に見てもらう
    throw "AviUtl2 が $DialogSeconds 秒で閉じない 確かめる窓が出ていないか画面を見てください"
}

if (@(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Count) {
    # 入口でも見ているが、入口からここまでの間に開かれることもある 作業中の物は触らない
    Say 'AviUtl2 が既に開いています 閉じてから走らせてください'
    exit 1
}

if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
    Say "書き出す先のフォルダ $Folder がありません"
    exit 1
}
if ((Get-Pictures).Count) {
    # 前の回の絵が混ざると、枚数が揃ったように見えて書き終える前に止めてしまう
    Say "書き出す先のフォルダ $Folder に PNG が既にあります 空のフォルダを渡してください"
    exit 1
}

$exitCode = 1
$main = $null
try {
    Say "AviUtl2 を起動します $Aviutl2"
    # 引数は 1 つの文字列で渡す 空白を含むパスを引用符で囲まないと、別々の引数に割れる
    $script:Aviutl2Process = Start-Process -FilePath $Aviutl2 -ArgumentList ('"' + $Project + '"') -PassThru
    $main = Wait-MainWindow
    Say "プロジェクトが開きました $ProjectName"
    # 題名が変わった直後は、メニューや保存の窓の用意がまだのことがある
    Start-Sleep -Seconds 2
    $command = Find-MenuCommand $main
    [void]$NativeApi::Post([System.IntPtr]$main.Current.NativeWindowHandle, $WM_COMMAND, [System.IntPtr]$command, [System.IntPtr]::Zero)
    Save-Sequence $main ([System.IO.Path]::Combine($Folder, "$BaseName.png"))
    Wait-Written
    Say "書き出しました $((Get-Pictures).Count) 枚"
    $exitCode = 0
} catch {
    Say "書き出せませんでした $($_.Exception.Message)"
    $exitCode = 1
} finally {
    if ($script:Aviutl2Process) {
        try {
            if ($main) { Close-Dialog $main }
            Close-Aviutl2
        } catch {
            Say "AviUtl2 を閉じられませんでした $($_.Exception.Message)"
            $exitCode = 1
        }
    }
}
exit $exitCode
