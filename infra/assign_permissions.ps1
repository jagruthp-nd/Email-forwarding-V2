# =============================================================================
# EF Automation - Assign Microsoft Graph API Permissions to Managed Identity
#
# Run this in PowerShell AFTER setup.sh completes.
# Prerequisites:
#   Install-Module AzureAD  (or use the Microsoft.Graph module)
#   Connect-AzureAD
# =============================================================================

param(
    [Parameter(Mandatory = $true)]
    [string]$PrincipalId   # Managed Identity Principal ID from setup.sh output
)

# Connect to Azure AD (interactive login)
Write-Host "Connecting to Azure AD..." -ForegroundColor Cyan
Connect-AzureAD

# Get the Microsoft Graph service principal
$GraphAppId = "00000003-0000-0000-c000-000000000000"
$GraphSp = Get-AzureADServicePrincipal -Filter "appId eq '$GraphAppId'"

Write-Host "Microsoft Graph Service Principal ID: $($GraphSp.ObjectId)" -ForegroundColor Green

# Define required permissions
$requiredRoles = @(
    "Directory.Read.All",                       # Read terminated users from Azure AD
    "User.ReadWrite.All",                       # Delete user accounts
    "MailboxSettings.ReadWrite",                # Read/update mailbox forwarding settings
    "CustomSecAttributeAssignment.ReadWrite.All" # Read/write Custom Security Attributes (Workflow B)
)

foreach ($roleName in $requiredRoles) {
    $appRole = $GraphSp.AppRoles | Where-Object { $_.Value -eq $roleName -and $_.AllowedMemberTypes -contains "Application" }

    if ($null -eq $appRole) {
        Write-Warning "Role '$roleName' not found in Microsoft Graph. Skipping."
        continue
    }

    # Check if already assigned
    $existing = Get-AzureADServiceAppRoleAssignment -ObjectId $PrincipalId |
        Where-Object { $_.Id -eq $appRole.Id }

    if ($existing) {
        Write-Host "Role '$roleName' already assigned. Skipping." -ForegroundColor Yellow
    } else {
        New-AzureADServiceAppRoleAssignment `
            -ObjectId    $PrincipalId `
            -PrincipalId $PrincipalId `
            -ResourceId  $GraphSp.ObjectId `
            -Id          $appRole.Id

        Write-Host "Granted: $roleName" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Graph API permissions assigned successfully." -ForegroundColor Green
Write-Host "Permissions granted:" -ForegroundColor Cyan
$requiredRoles | ForEach-Object { Write-Host "  - $_" }
Write-Host ""
Write-Host "IMPORTANT – Additional manual step for Custom Security Attributes:" -ForegroundColor Yellow
Write-Host "  1. Open Azure AD admin center → Custom Security Attributes" -ForegroundColor Yellow
Write-Host "  2. Create attribute set:  EFAutomation  (or the value of CSA_ATTRIBUTE_SET)" -ForegroundColor Yellow
Write-Host "  3. Create attribute:      ExtensionStatus  (String, single-value)" -ForegroundColor Yellow
Write-Host "  4. Assign 'Attribute Assignment Administrator' role to the IT Engineers" -ForegroundColor Yellow
Write-Host "     who will approve extension requests." -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
