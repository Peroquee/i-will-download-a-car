*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Input Page

*** Test Cases ***
The Navigation Opens Every Page
    Click Element    id:nav-dashboard
    Wait Until Element Is Visible    id:dashboard
    Location Should Contain    /dashboard
    Element Attribute Value Should Be    id:nav-dashboard    aria-current    page
    Click Element    id:nav-subs
    Wait Until Element Is Visible    id:subscriptions
    Element Attribute Value Should Be    id:nav-subs    aria-current    page
    Click Element    id:nav-accounts
    Wait Until Element Is Visible    id:accounts
    Element Attribute Value Should Be    id:nav-accounts    aria-current    page
    Click Element    id:nav-input
    Wait Until Element Is Visible    id:form

The VPN Bar Opens The VPN Section
    Click Element    id:vpn-link
    Wait Until Element Is Visible    id:dashboard
    Location Should Contain    /dashboard
    ${url}=    Get Location
    Should Contain    ${url}    /dashboard#vpn
    ${text}=    Get Text    id:dash-vpn-explain
    Should Not Be Empty    ${text}

The How To Guide Opens And Closes
    Click Element    id:howto-open
    Wait Until Element Is Visible    id:howto
    Element Should Be Visible    id:howto-title
    Click Button    id:howto-close
    Wait Until Element Is Not Visible    id:howto

The Language Switch Changes The Interface And Survives A Reload
    ${before}=    Get Text    id:nav-input
    Click Element    id:lang
    Wait Until Keyword Succeeds    5s    0.2s    Language Differs    ${before}
    ${after}=    Get Text    id:nav-input
    Reload Page
    Wait Until Element Is Visible    id:nav-input
    Element Should Contain    id:nav-input    ${after}
    Click Element    id:lang
    Wait Until Keyword Succeeds    5s    0.2s    Element Should Contain    id:nav-input    ${before}

Every Page Shows Its Headline
    Assert Page Headline    input-title    Input
    Click Element    id:nav-dashboard
    Assert Page Headline    dashboard-title    Dashboard
    Click Element    id:nav-subs
    Assert Page Headline    subs-title    Subscriptions
    Click Element    id:nav-accounts
    Assert Page Headline    accounts-title    Accounts

The Desktop Sidebar Stays At The Left Edge
    ${logo_x}=    Execute JavaScript    return document.querySelector('.logo').getBoundingClientRect().left
    Should Be True    ${logo_x} < 60
    ${nav_x}=    Execute JavaScript    return document.querySelector('#topnav').getBoundingClientRect().left
    Should Be True    ${nav_x} < 60

A Phone Layout Does Not Scroll Sideways
    Set Window Size    390    900
    Go To    ${BASE_URL}/
    Wait Until Element Is Visible    id:form
    ${fits}=    Execute JavaScript    return document.documentElement.scrollWidth <= window.innerWidth + 1
    Should Be True    ${fits}
    ${size}=    Get Element Size    id:nav-input
    Should Be True    ${size}[1] >= 44
    Set Window Size    1280    900
