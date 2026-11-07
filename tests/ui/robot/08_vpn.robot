*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Input Page

*** Test Cases ***
The Vpn Bar Shows The Running State
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:vpn-text    VPN running
    ${cls}=    Get Element Attribute    id:vpn    class
    Should Contain    ${cls}    vpn-ok

The Vpn Part Explains The State
    Click Element    id:vpn-link
    Wait Until Element Is Visible    id:dashboard
    Element Should Contain    id:dash-vpn-now    VPN running
    ${text}=    Get Text    id:dash-vpn-explain
    Should Not Be Empty    ${text}
