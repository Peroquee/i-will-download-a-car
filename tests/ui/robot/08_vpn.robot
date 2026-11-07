*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Input Page

*** Test Cases ***
The Vpn Indicator Shows The Running State
    Wait Until Keyword Succeeds    15s    0.5s    The Vpn Indicator Is Ok
    ${label}=    Get Element Attribute    id:vpn-indicator    aria-label
    Should Contain    ${label}    VPN running
    Element Should Not Be Visible    id:vpn
    ${size}=    Get Element Size    id:vpn-indicator
    Should Be True    ${size}[0] >= 32
    Should Be True    ${size}[1] >= 32

The Vpn Tab Shows The Current State
    Click Element    id:vpn-indicator
    Wait Until Element Is Visible    id:dashboard
    Wait Until Element Is Visible    id:dash-vpn
    Element Should Contain    id:dash-vpn-now    VPN running
    Element Attribute Value Should Be    id:dash-tab-vpn    aria-current    page

*** Keywords ***
The Vpn Indicator Is Ok
    ${cls}=    Get Element Attribute    id:vpn-indicator    class
    Should Contain    ${cls}    vpn-ok
