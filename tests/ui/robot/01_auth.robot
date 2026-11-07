*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App
Suite Teardown    Close All Browsers

*** Test Cases ***
A Visitor Sees Only The Sign In Form
    Go To Login
    Page Should Contain Element    id:login-form
    Element Should Not Be Visible    id:topnav
    Element Should Not Be Visible    id:form

A Wrong Password Is Refused
    Go To Login
    Input Text    id:login-user    ${ADMIN_USER}
    Input Password    id:login-password    definitely not the password
    Click Button    id:login-submit
    Assert Field Error    login-error    Wrong name or password.
    Element Should Be Visible    id:login-form

The Admin Signs In And Sees The Input Page
    Go To Login
    Sign In As Admin
    Wait Until Element Is Visible    id:form
    Assert Page Headline    input-title    Input
    Element Should Contain    id:account-open    B
    Element Should Be Visible    id:topnav

The Account Dialog Shows The Signed In User
    Open Input Page
    Click Element    id:account-open
    Wait Until Element Is Visible    id:account
    Element Should Contain    id:account-who    boss
    Element Should Contain    id:account-who    Administrator
    Click Button    id:account-close
    Wait Until Element Is Not Visible    id:account

Logout Ends The Session
    Open Input Page
    Log Out
    Element Should Be Visible    id:login-form
    Go To    ${BASE_URL}/dashboard
    Wait Until Element Is Visible    id:login-form

Password Change Validation Is Shown
    Open Input Page
    Click Element    id:account-open
    Wait Until Element Is Visible    id:account
    Input Password    id:account-current    ${ADMIN_PASSWORD}
    Input Password    id:account-new    short
    Input Password    id:account-repeat    short
    Click Button    id:account-change
    Wait Until Keyword Succeeds    5s    0.2s    Element Should Contain    id:account-result    at least 12 characters
    Input Password    id:account-current    ${ADMIN_PASSWORD}
    Input Password    id:account-new    a long new password
    Input Password    id:account-repeat    a different password
    Click Button    id:account-change
    Wait Until Keyword Succeeds    5s    0.2s    Element Should Contain    id:account-result    two new passwords differ
    Click Button    id:account-close
