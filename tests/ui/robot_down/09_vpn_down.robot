*** Settings ***
Resource    ../robot/resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers

*** Test Cases ***
The Vpn Down State Is Unmistakable
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:vpn-text    VPN down
    Element Should Be Visible    id:vpn
    ${cls}=    Get Element Attribute    id:vpn    class
    Should Contain    ${cls}    vpn-banner vpn-down
    ${indicator}=    Get Element Attribute    id:vpn-indicator    class
    Should Contain    ${indicator}    vpn-down
    ${title}=    Execute JavaScript    return document.title
    Should Start With    ${title}    ⚠
    ${icon}=    Get Element Attribute    id:favicon    href
    Should Contain    ${icon}    favicon-alert.svg
