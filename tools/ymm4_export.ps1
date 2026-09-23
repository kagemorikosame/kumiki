<#
YMM4 に .ymmp を書き出させる UI Automation の本体（tools/ymm4_export.py から呼ぶ）

手で走らせず、入口の ymm4_export.py を使う 入口が YMM4 の場所を探し、既に開いて
いないかを確かめ、前の書き出しを片付けてからここを呼ぶ

YMM4 4.56.1.1 で確かめた手順（Issue #116）
  1. YukkuriMovieMaker.exe "<.ymmp>" で起動し、下の帯にプロジェクトの名前が出るまで待つ
  2. 〔ファイル(F)〕→〔動画出力〕 書き出しの窓（主の窓の子の Window）の〔出力〕を押す
  3. 「名前を付けて保存」の名前の欄へ WM_SETTEXT で書き、〔保存〕を BM_CLICK で押す
  4. 進み具合の窓が消え、出力の大きさが止まって書き手が手放すまで待つ
  5. 主の窓を閉じる

Windows PowerShell 5.1 の罠
  - BOM の無い .ps1 は Shift_JIS として読まれ、日本語の名前が全部化ける このファイルは
    BOM 付きの UTF-8 で置く（試験で見ている）
  - [System.Windows.Automation.…] と書いた型は読む時点で解決しようとして、Add-Type の
    前だと見つからない 型は Add-Type のあとで文字列から引く
  - 変数名は大文字小文字を区別しない（$c と $C は同じ物） 1 文字の名前を使わない
  - 標準出力へ書く文字は、窓が無いと Shift_JIS になる Say で UTF-8 のまま書き、
    入口が UTF-8 として読む

終了コード 0 書き出せた / 1 書き出せなかった / 2 コンプレッサーの設定を戻せなかった
#>
param(
    [Parameter(Mandatory = $true)][string]$Ymm4,
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Output,
    [switch]$NoCompressor,
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'

$script:StandardOutput = [Console]::OpenStandardOutput()

function Say([string]$Text) {
    # Write-Host は窓の無いときに Shift_JIS で書く 入口はパイプで受けるので、
    # そのままだと入口の側で日本語が化ける バイト列で UTF-8 のまま渡す
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text + "`n")
    $script:StandardOutput.Write($bytes, 0, $bytes.Length)
    $script:StandardOutput.Flush()
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace SashimonoYmm4 -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "SendMessageW", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr SendText(System.IntPtr hWnd, int msg, System.IntPtr wParam, string lParam);
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "SendMessageW")]
public static extern System.IntPtr SendPlain(System.IntPtr hWnd, int msg, System.IntPtr wParam, System.IntPtr lParam);
[System.Runtime.InteropServices.DllImport("user32.dll", EntryPoint = "GetWindowTextW", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern int ReadText(System.IntPtr hWnd, System.Text.StringBuilder text, int count);
'@

# 型は Add-Type のあとで引く（読む時点で解決させると見つからない）
$Uia = 'System.Windows.Automation.AutomationElement' -as [type]
$Scope = 'System.Windows.Automation.TreeScope' -as [type]
$ControlTypes = 'System.Windows.Automation.ControlType' -as [type]
$Everything = ('System.Windows.Automation.Condition' -as [type])::TrueCondition
$ExpandPattern = ('System.Windows.Automation.ExpandCollapsePattern' -as [type])::Pattern
$InvokePattern = ('System.Windows.Automation.InvokePattern' -as [type])::Pattern
$SelectItemPattern = ('System.Windows.Automation.SelectionItemPattern' -as [type])::Pattern
$WindowPattern = ('System.Windows.Automation.WindowPattern' -as [type])::Pattern
$NativeApi = 'SashimonoYmm4.Native' -as [type]

$WM_SETTEXT = 0x000C
$BM_CLICK = 0x00F5

$ProcessName = [System.IO.Path]::GetFileNameWithoutExtension($Ymm4)
$ProjectStem = [System.IO.Path]::GetFileNameWithoutExtension($Project)
$ProgressName = 'YukkuriMovieMaker.ViewModels.ProgressViewModel'
$SaveDialogName = '名前を付けて保存'
$CompressorGroup = '音量調整 / 音割れ対策（コンプレッサー）'
$VolumeLabel = '音量調整'
$CompressionLabel = 'コンプレッサー'
$VolumeOff = '音量調整しない'
$CompressionOff = '圧縮しない'

# 起動とプロジェクトの読み込み 大きなプロジェクトや初回の起動は 1 分を超えることがある
$LoadSeconds = 180
# 押してから窓が出るまで ここで出なければ、手順がどこかで食い違っている
$DialogSeconds = 30
# 大きさがこの秒数止まり、書き手が手放したら書き終えたとみなす
# 進み具合の窓が消えたあとも書き出しが続いていたことがあった（2026-09-23）
$SettleSeconds = 5

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

function Find-Named($Root, [string]$Name, $ControlType) {
    $condition = New-AndCondition (New-Condition $Uia::NameProperty $Name) (New-Condition $Uia::ControlTypeProperty $ControlType)
    return $Root.FindFirst($Scope::Descendants, $condition)
}

function Get-Label($Element) {
    # コンボボックスの項目は、中の文字の要素に名前がある 子孫の名前をつないで 1 つの札にする
    $names = foreach ($inner in $Element.FindAll($Scope::Descendants, $Everything)) {
        $inner.Current.Name
    }
    return (@($names) | Where-Object { $_ }) -join '/'
}

function Get-TopWindows {
    $windows = @()
    foreach ($running in @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)) {
        $windows += @($Uia::RootElement.FindAll($Scope::Children, (New-Condition $Uia::ProcessIdProperty $running.Id)))
    }
    return $windows
}

function Test-ProjectShown($Window) {
    # 下の帯にプロジェクトの名前が出ていれば読み込み終わり
    # 起動の途中の窓（読み込み中の表示など）には下の帯が無い
    foreach ($bar in $Window.FindAll($Scope::Descendants, (New-Condition $Uia::ControlTypeProperty $ControlTypes::StatusBar))) {
        foreach ($text in $bar.FindAll($Scope::Descendants, (New-Condition $Uia::ControlTypeProperty $ControlTypes::Text))) {
            if ($text.Current.Name.Contains($ProjectStem)) { return $true }
        }
    }
    return $false
}

function Wait-MainWindow {
    return Wait-For {
        foreach ($window in Get-TopWindows) {
            if (Test-ProjectShown $window) { return $window }
        }
        return $null
    } $LoadSeconds "YMM4 の下の帯の「$ProjectStem」"
}

function Get-ExportDialog($Main) {
    # 書き出しの窓は主の窓の子の Window 進み具合の窓も同じ作りなので、〔出力〕の有無で分ける
    foreach ($child in $Main.FindAll($Scope::Children, (New-Condition $Uia::ClassNameProperty 'Window'))) {
        if (Find-Named $child '出力' $ControlTypes::Button) { return $child }
    }
    return $null
}

function Open-ExportDialog($Main) {
    $open = Get-ExportDialog $Main
    if ($open) { return $open }
    $fileMenu = Wait-For { Find-Named $Main 'ファイル(F)' $ControlTypes::MenuItem } $DialogSeconds '〔ファイル(F)〕'
    $fileMenu.GetCurrentPattern($ExpandPattern).Expand()
    $exportItem = Wait-For {
        foreach ($item in $fileMenu.FindAll($Scope::Descendants, (New-Condition $Uia::ControlTypeProperty $ControlTypes::MenuItem))) {
            if ($item.Current.Name.StartsWith('動画出力')) { return $item }
        }
        return $null
    } $DialogSeconds '〔動画出力〕'
    $exportItem.GetCurrentPattern($InvokePattern).Invoke()
    return Wait-For { Get-ExportDialog $Main } $DialogSeconds '書き出しの窓'
}

function Close-ExportDialog($Main, $Dialog) {
    # 窓の × と同じ閉じ方 選んだ設定はこれで残る（2026-09-23 に確かめた）
    $Dialog.GetCurrentPattern($WindowPattern).Close()
    [void](Wait-For { -not (Get-ExportDialog $Main) } $DialogSeconds '書き出しの窓が閉じること')
}

function Get-ComboAfter($Dialog, [string]$Label) {
    # 札の文字と選ぶ欄は兄弟で並ぶ 札の後ろに出てくる最初のコンボボックスが、その札の欄
    $seen = $false
    foreach ($element in $Dialog.FindAll($Scope::Descendants, $Everything)) {
        $info = $element.Current
        if ($info.ControlType -eq $ControlTypes::Text -and $info.Name -eq $Label) { $seen = $true; continue }
        if ($seen -and $info.ControlType -eq $ControlTypes::ComboBox) { return $element }
    }
    throw "書き出しの窓に「$Label」の欄が見つからない"
}

function Read-Combo($Combo) {
    # 閉じたままだと項目が作られていないことがある 開いて選ばれている物を読む
    $expander = $Combo.GetCurrentPattern($ExpandPattern)
    $expander.Expand()
    Start-Sleep -Milliseconds 500
    $chosen = $null
    try {
        foreach ($item in $Combo.FindAll($Scope::Children, $Everything)) {
            $selected = $false
            try { $selected = $item.GetCurrentPattern($SelectItemPattern).Current.IsSelected } catch { $selected = $false }
            if ($selected) { $chosen = Get-Label $item; break }
        }
    } finally {
        $expander.Collapse()
        Start-Sleep -Milliseconds 300
    }
    if (-not $chosen) { throw 'コンボボックスの選ばれている項目が読めない' }
    return $chosen
}

function Select-Combo($Combo, [string]$Wanted) {
    $expander = $Combo.GetCurrentPattern($ExpandPattern)
    $expander.Expand()
    Start-Sleep -Milliseconds 500
    $done = $false
    try {
        foreach ($item in $Combo.FindAll($Scope::Children, $Everything)) {
            if ((Get-Label $item) -eq $Wanted) {
                $item.GetCurrentPattern($SelectItemPattern).Select()
                $done = $true
                break
            }
        }
        Start-Sleep -Milliseconds 300
    } finally {
        # 選べなくても開いたままにしない 開いたままだと次の操作が一覧に吸われる
        try { $expander.Collapse() } catch { }
        Start-Sleep -Milliseconds 300
    }
    if (-not $done) { throw "選択肢「$Wanted」が見つからない" }
}

function Open-CompressorGroup($Dialog) {
    $group = Find-Named $Dialog $CompressorGroup $ControlTypes::Group
    if (-not $group) { throw "書き出しの窓に〔$CompressorGroup〕が見つからない" }
    $expander = $group.GetCurrentPattern($ExpandPattern)
    if ($expander.Current.ExpandCollapseState -ne 'Expanded') {
        $expander.Expand()
        Start-Sleep -Milliseconds 500
    }
}

function Read-Compressor($Dialog) {
    Open-CompressorGroup $Dialog
    return @{
        Volume = Read-Combo (Get-ComboAfter $Dialog $VolumeLabel)
        Compression = Read-Combo (Get-ComboAfter $Dialog $CompressionLabel)
    }
}

function Set-Compressor($Dialog, [string]$Volume, [string]$Compression) {
    Open-CompressorGroup $Dialog
    Select-Combo (Get-ComboAfter $Dialog $VolumeLabel) $Volume
    Select-Combo (Get-ComboAfter $Dialog $CompressionLabel) $Compression
    $now = Read-Compressor $Dialog
    if ($now.Volume -ne $Volume -or $now.Compression -ne $Compression) {
        throw "選んだはずの値にならない 音量調整「$($now.Volume)」 コンプレッサー「$($now.Compression)」"
    }
}

function Close-SaveDialog($Main) {
    # 失敗の途中で保存の窓が残っていると、主の窓が何も受け付けない 取り消して閉じる
    $saveDialog = $Main.FindFirst($Scope::Children, (New-Condition $Uia::NameProperty $SaveDialogName))
    if (-not $saveDialog) { return }
    $cancel = $saveDialog.FindFirst($Scope::Descendants, (New-AndCondition (New-Condition $Uia::AutomationIdProperty '2') (New-Condition $Uia::ClassNameProperty 'Button')))
    if ($cancel) {
        [void]$NativeApi::SendPlain([System.IntPtr]$cancel.Current.NativeWindowHandle, $BM_CLICK, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
    }
}

function Restore-Compressor($Main, $Original) {
    Close-SaveDialog $Main
    $dialog = Open-ExportDialog $Main
    Set-Compressor $dialog $Original.Volume $Original.Compression
    Close-ExportDialog $Main $dialog
    # 閉じたあとも残っているかは、開き直して読まないと分からない
    $dialog = Open-ExportDialog $Main
    $after = Read-Compressor $dialog
    Close-ExportDialog $Main $dialog
    if ($after.Volume -ne $Original.Volume -or $after.Compression -ne $Original.Compression) {
        throw "開き直すと 音量調整「$($after.Volume)」 コンプレッサー「$($after.Compression)」 だった"
    }
}

function Save-As($Main) {
    $saveDialog = Wait-For { $Main.FindFirst($Scope::Children, (New-Condition $Uia::NameProperty $SaveDialogName)) } $DialogSeconds '「名前を付けて保存」'
    # 名前の欄は ValuePattern を持たない Win32 の Edit なので、窓へ直接書く
    $nameBox = Wait-For {
        $saveDialog.FindFirst($Scope::Descendants, (New-AndCondition (New-Condition $Uia::AutomationIdProperty '1001') (New-Condition $Uia::ClassNameProperty 'Edit')))
    } $DialogSeconds '保存の名前の欄'
    $nameHandle = [System.IntPtr]$nameBox.Current.NativeWindowHandle
    [void]$NativeApi::SendText($nameHandle, $WM_SETTEXT, [System.IntPtr]::Zero, $Output)
    $buffer = New-Object System.Text.StringBuilder 4096
    [void]$NativeApi::ReadText($nameHandle, $buffer, $buffer.Capacity)
    if ($buffer.ToString() -ne $Output) {
        # 違う名前のまま保存すると、別の所へ書き出して「見つからない」で終わる
        throw "保存の名前の欄に書けない（読み返すと「$($buffer.ToString())」）"
    }
    $saveButton = $saveDialog.FindFirst($Scope::Descendants, (New-AndCondition (New-Condition $Uia::AutomationIdProperty '1') (New-Condition $Uia::ClassNameProperty 'Button')))
    if (-not $saveButton) { throw '〔保存〕が見つからない' }
    [void]$NativeApi::SendPlain([System.IntPtr]$saveButton.Current.NativeWindowHandle, $BM_CLICK, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
}

function Test-Released {
    # 書き手が開いたままなら、共有なしでは開けない 開けたら YMM4 は書き終えて手放している
    try {
        $stream = [System.IO.File]::Open($Output, 'Open', 'Read', 'None')
        $stream.Dispose()
        return $true
    } catch {
        return $false
    }
}

function Wait-Written($Main) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    [void](Wait-For {
        ($Main.FindFirst($Scope::Children, (New-Condition $Uia::NameProperty $ProgressName))) -or (Test-Path -LiteralPath $Output)
    } $DialogSeconds '書き出しの始まり（進み具合の窓か出力のファイル）')
    Say '書き出しています'
    while ($Main.FindFirst($Scope::Children, (New-Condition $Uia::NameProperty $ProgressName))) {
        if ((Get-Date) -gt $deadline) { throw "書き出しが $TimeoutSeconds 秒で終わらない" }
        Start-Sleep -Seconds 1
    }
    $lastSize = -1
    $stableSince = $null
    while ($true) {
        if ((Get-Date) -gt $deadline) { throw "書き出しが $TimeoutSeconds 秒で終わらない（出力の大きさが止まらない）" }
        $size = -1
        if (Test-Path -LiteralPath $Output) { $size = (Get-Item -LiteralPath $Output).Length }
        if ($size -gt 0 -and $size -eq $lastSize) {
            if (-not $stableSince) { $stableSince = Get-Date }
            if (((Get-Date) - $stableSince).TotalSeconds -ge $SettleSeconds -and (Test-Released)) { return }
        } else {
            $stableSince = $null
        }
        $lastSize = $size
        Start-Sleep -Seconds 1
    }
}

function Close-Ymm4 {
    foreach ($window in Get-TopWindows) {
        try { $window.GetCurrentPattern($WindowPattern).Close() } catch { }
    }
    $until = (Get-Date).AddSeconds($DialogSeconds)
    while ((Get-Date) -lt $until) {
        if (-not @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Count) { return }
        Start-Sleep -Milliseconds 500
    }
    # 無理に止めない 止めると YMM4 が設定を書き残す前に終わり、戻した設定が消えかねない
    throw "YMM4 が $DialogSeconds 秒で閉じない 確かめる窓が出ていないか画面を見てください"
}

if (@(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Count) {
    # 入口でも見ているが、入口から ここまでの間に開かれることもある 作業中の物は触らない
    Say 'YMM4 が既に開いています 閉じてから走らせてください'
    exit 1
}

$exitCode = 1
$started = $false
$main = $null
$original = $null
try {
    Say "YMM4 を起動します $Ymm4"
    # 引数は 1 つの文字列で渡す 空白を含むパスを引用符で囲まないと、別々の引数に割れる
    [void](Start-Process -FilePath $Ymm4 -ArgumentList ('"' + $Project + '"') -PassThru)
    $started = $true
    $main = Wait-MainWindow
    Say "プロジェクトが開きました $ProjectStem"
    # 帯に名前が出た直後は、メニューがまだ組み上がっていないことがある
    Start-Sleep -Seconds 2
    $dialog = Open-ExportDialog $main
    if ($NoCompressor) {
        $before = Read-Compressor $dialog
        Say "コンプレッサーの元の値 音量調整「$($before.Volume)」 コンプレッサー「$($before.Compression)」"
        if ($before.Volume -ne $VolumeOff -or $before.Compression -ne $CompressionOff) {
            # 変える前に覚える 途中で落ちても finally で戻せるように
            $original = $before
            Set-Compressor $dialog $VolumeOff $CompressionOff
            Say "コンプレッサーを切りました 音量調整「$VolumeOff」 コンプレッサー「$CompressionOff」"
        }
    }
    $exportButton = Find-Named $dialog '出力' $ControlTypes::Button
    if (-not $exportButton) { throw '書き出しの窓に〔出力〕が見つからない' }
    $exportButton.GetCurrentPattern($InvokePattern).Invoke()
    Save-As $main
    Wait-Written $main
    Say "書き出しました $Output"
    $exitCode = 0
} catch {
    Say "書き出せませんでした $($_.Exception.Message)"
    $exitCode = 1
} finally {
    if ($original) {
        try {
            Restore-Compressor $main $original
            Say "コンプレッサーを元へ戻しました 音量調整「$($original.Volume)」 コンプレッサー「$($original.Compression)」"
        } catch {
            Say '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!'
            Say "!!! コンプレッサーの設定を戻せませんでした $($_.Exception.Message)"
            Say "!!! YMM4 の書き出しの窓で 音量調整を「$($original.Volume)」、コンプレッサーを「$($original.Compression)」へ手で戻してください"
            Say '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!'
            $exitCode = 2
        }
    }
    if ($started) {
        try {
            Close-Ymm4
        } catch {
            Say "YMM4 を閉じられませんでした $($_.Exception.Message)"
            if ($exitCode -eq 0) { $exitCode = 1 }
        }
    }
}
exit $exitCode
