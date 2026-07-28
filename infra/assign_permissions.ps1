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
    "User.ReadWrite.All",                       # Delete user accounts + remove licenses (POST /users/{id}/assignLicense)
    "MailboxSettings.ReadWrite",                # Read/update mailbox forwarding + OOO settings
    "CustomSecAttributeAssignment.ReadWrite.All" # Read/write Custom Security Attributes (Workflow B)
    # Note: User.Read.All is covered by User.ReadWrite.All (superset).
    # The Graph beta endpoint /users/{id}?$select=inPlaceHolds (litigation hold check)
    # requires User.Read.All which is already included in User.ReadWrite.All.
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
Write-Host "IMPORTANT – One-time manual setup for Custom Security Attributes (CSA):" -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 1 – Create the Attribute Set and Attribute:" -ForegroundColor Yellow
Write-Host "    a. Open Azure AD admin center → Custom Security Attributes" -ForegroundColor Yellow
Write-Host "    b. Create attribute set:  EFAutomation  (matches env var CSA_ATTRIBUTE_SET)" -ForegroundColor Yellow
Write-Host "    c. Within EFAutomation, create attribute:" -ForegroundColor Yellow
Write-Host "         Name:                  ExtStatus        (matches env var CSA_ATTRIBUTE_NAME)" -ForegroundColor Yellow
Write-Host "         Data type:             String" -ForegroundColor Yellow
Write-Host "         Multi-value:           No" -ForegroundColor Yellow
Write-Host "         Allow only predefined: Yes  <-- enforces dropdown for IT Engineers" -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 2 – Add predefined values to ExtStatus:" -ForegroundColor Yellow
Write-Host "    Add the following values (these are the ONLY values IT Engineers will see):" -ForegroundColor Yellow
Write-Host "         EF_ALERT_SENT   (written by automation when alert email is sent)" -ForegroundColor Yellow
Write-Host "         Extend_to_30    (IT selects to approve 1st extension – deadline Day 30→60)" -ForegroundColor Yellow
Write-Host "         Extended_to_60  (IT selects to approve 2nd extension – deadline Day 60→90)" -ForegroundColor Yellow
Write-Host "         Extended_MAX    (IT selects to approve final extension – deadline Day 90)" -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 3 – Assign roles:" -ForegroundColor Yellow
Write-Host "    a. Assign 'Attribute Assignment Administrator' to IT Engineers who will" -ForegroundColor Yellow
Write-Host "       approve extension requests (so they can set the CSA via portal)." -ForegroundColor Yellow
Write-Host "    b. The Function App Managed Identity already receives" -ForegroundColor Yellow
Write-Host "       'CustomSecAttributeAssignment.ReadWrite.All' (assigned above)." -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  NOTE: The automation writes 'EF_ALERT_SENT' directly via API.  Because" -ForegroundColor Yellow
Write-Host "  'Allow only predefined values' is enabled, this value MUST be in the" -ForegroundColor Yellow
Write-Host "  predefined list (Step 2) or the Graph API call will fail." -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 4 – Add TicketRef CSA attribute:" -ForegroundColor Yellow
Write-Host "    In the same EFAutomation attribute set, create a second attribute:" -ForegroundColor Yellow
Write-Host "         Name:                  TicketRef        (matches env var CSA_TICKET_REF_NAME)" -ForegroundColor Yellow
Write-Host "         Data type:             String" -ForegroundColor Yellow
Write-Host "         Multi-value:           No" -ForegroundColor Yellow
Write-Host "         Allow only predefined: No  <-- free-text, IT types the SD+ ticket number" -ForegroundColor Yellow
Write-Host "    This attribute is set by the IT approval webhook when a ticket ref is entered." -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 5 – License removal (User.ReadWrite.All covers this):" -ForegroundColor Yellow
Write-Host "    POST /users/{id}/assignLicense is used to strip M365 licenses post-deletion." -ForegroundColor Yellow
Write-Host "    No additional permission beyond User.ReadWrite.All is required." -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Step 6 – Litigation hold check (Graph beta):" -ForegroundColor Yellow
Write-Host "    GET /beta/users/{id}?`$select=inPlaceHolds requires User.Read.All," -ForegroundColor Yellow
Write-Host "    which is a subset of User.ReadWrite.All (already assigned above)." -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
