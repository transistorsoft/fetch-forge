# The fetch lab's `Lab` native module as a local development pod (WO-002 phase 2), after bg-forge's React Native
# lab: the Podfile's `pod 'LabModule', :path => 'LabModule'` compiles and links it without an edit to the project
# file.  Never published.
Pod::Spec.new do |s|
  s.name         = 'LabModule'
  s.version      = '0.0.1'
  s.summary      = 'Scenario args, the native probe and the host verdict for the React Native fetch lab (iOS).'
  s.homepage     = 'https://github.com/transistorsoft'
  s.license      = { :type => 'Commercial' }
  s.author       = 'Transistor Software'
  s.source       = { :path => '.' }
  s.platform     = :ios, '15.1'
  s.source_files = 'LabModule.m'
  s.dependency 'React-Core'
  s.dependency 'TSBackgroundFetch'
end
