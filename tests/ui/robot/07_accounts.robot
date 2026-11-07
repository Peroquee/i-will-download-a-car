*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Accounts Page

*** Test Cases ***
The Accounts Page Lists The Admin And The Seeded User
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:accounts-list    alice
    Element Should Contain    id:accounts-list    boss

A User Can Be Created
    Input Text    id:accounts-name    carol
    Input Password    id:accounts-password    a long carol password
    Select From List By Value    id:accounts-role    user
    Click Button    id:accounts-add
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Contain    id:accounts-result    Account "carol" created.
    Wait Until Element Is Visible    xpath=//li[contains(@class,'job')][contains(.,'carol')]

A Role Can Be Changed
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'carol')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Make administrator']
    Click Button    ${row}//button[normalize-space(.)='Make administrator']
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Contain    ${row}    Administrator
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Make user']
    Click Button    ${row}//button[normalize-space(.)='Make user']
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Contain    ${row}    User

An Account Can Be Disabled And Enabled
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'carol')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Disable']
    Click Button    ${row}//button[normalize-space(.)='Disable']
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Contain    ${row}    disabled
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Enable']
    Click Button    ${row}//button[normalize-space(.)='Enable']
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Not Contain    ${row}    disabled

A Password Can Be Reset
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'carol')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Set password']
    Click Button    ${row}//button[normalize-space(.)='Set password']
    Wait Until Element Is Visible    ${row}//input[@type='password']
    Input Password    ${row}//input[@type='password']    a long carol password two
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Save password']
    Click Button    ${row}//button[normalize-space(.)='Save password']
    Wait Until Keyword Succeeds    10s    0.5s    Element Should Contain    ${row}    The password is set

An Account Can Be Deleted
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'carol')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Delete']
    Click Button    ${row}//button[normalize-space(.)='Delete']
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Really delete?']
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Really delete?']
    Click Button    ${row}//button[normalize-space(.)='Really delete?']
    Wait Until Keyword Succeeds    10s    0.5s    Page Should Not Contain    carol

A User Sees Fewer Tabs And No Admin Parts
    Log Out
    Sign In As    alice    ${ADMIN_PASSWORD}
    Element Should Not Be Visible    id:nav-accounts
    Open Dashboard Page
    Element Should Not Be Visible    id:dash-tab-alerts
    Click Element    id:dash-tab-vpn
    Wait Until Element Is Visible    id:dash-vpn
    Element Should Not Be Visible    id:dash-disk
    Element Should Not Be Visible    id:dash-vpn-list
    Log Out
