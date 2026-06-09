## Contributing

I don't care if AI is used as long as the code is sane and follows the project structure.   
  
### Git strategy

I'm loosely following the git flow strategy.   
  
`develop` is the default branch, so any features or bug fixes should branch off of that and PR's should point to there.

When `develop` reaches a point where I think it should be a new version, it'll get tagged and deployed.
`develop` should get merged into `main` any time there is a release. 
  
### Pull Requests

I don't care if you use AI as long as these general guidelines are followed:  
   
- The code needs to be scoped to a single feature or bug fix  
- All code additions need to be unit tested
- The `README` should be updated for any new features